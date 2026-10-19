"""The release planner and its edge: one repository, one distribution, CalVer versions."""
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from devtools import cli, release, release_check, release_check_index, release_rehearsal_gate
from devtools.release_check import Drift

_REAL_REHEARSAL_STEPS = cli._rehearsal_steps
V0, V1 = "2026.10.0", "2026.11.0"
_REPOSITORY_URL = "https://github.com/ppfenning/coxswain"


@pytest.fixture(autouse=True)
def _stdout_runner_follows_the_fake(monkeypatch):
    """The sync gate reads commit counts through `cli._stdout_run`; in these tests the
    patched `cli._real_run` stands in for it, so no test shells out to a real git."""
    # Late-bound on purpose: a test patches `cli._real_run` after this fixture runs.
    monkeypatch.setattr(cli, "_stdout_run", lambda argv, cwd: cli._real_run(argv, cwd))  # noqa: PLW0108


@pytest.fixture(autouse=True)
def _maintainer_checkout(monkeypatch):
    """Every `cli.main(["release", ...])` below runs as if it were on
    the maintainer's machine, unless a test overrides this to prove the guard."""
    monkeypatch.setattr(cli, "_maintainer_remote_url", lambda directory: "git@github.com:ppfenning/coxswain.git")
    monkeypatch.setattr(cli, "_tap_state", lambda directory: "clean")
    monkeypatch.setattr(cli, "_repository_url", lambda: _REPOSITORY_URL)


@pytest.fixture
def no_tap(monkeypatch):
    """A cut run end to end here stops before the tap step, which reads PyPI; its own tests drive it."""
    monkeypatch.setattr(release, "_tap_formula_steps", lambda *a, **k: [])


def _level(argv):
    """The fake's answer to the sync gate's `rev-list --left-right --count`: level with origin."""
    return (0, "0\t0\n") if "--left-right" in argv else None


def _is_tag_count(argv):
    """True for the edge's `rev-list <tag>..<ref> --count`, not the sync gate's left-right count."""
    return argv[3] == "rev-list" and "--count" in argv and "--left-right" not in argv


_MANIFEST_TOML = f"""
[coxswain]
version = "{V0}"

[components]
dash = {{ repo = "org/dash", tag = "v0.42.0", flag = "dash", provides = "towpath" }}
"""


def _manifest_toml_at(version: str) -> str:
    return _MANIFEST_TOML.replace(f'version = "{V0}"', f'version = "{version}"', 1)


def _manifest(current=V0, **dash):
    return {"coxswain": {"version": current},
            "components": {"dash": {"repo": "org/dash", "tag": "v0.42.0", "flag": "dash", **dash}}}


def _no_tags(manifest):
    """Every repository reachable with no tags — the clean case — the coxswain one included."""
    return {"coxswain": [], **{name: [] for name, spec in manifest["components"].items() if spec.get("repo")}}


def _kinds(steps):
    return [s["kind"] for s in steps]


def _checkout(tmp_path, manifest_toml=_MANIFEST_TOML, notes=None, version=V0):
    """The coxswain checkout at <tmp>/coxswain, holding the manifest (and, with `notes`, the release's notes page);
    dash and the tap sit beside it, the default `--root`."""
    umbrella = tmp_path / "coxswain"
    (umbrella / "docs" / "releases").mkdir(parents=True, exist_ok=True)
    (umbrella / "manifest.toml").write_text(manifest_toml)
    if notes is not None:
        (umbrella / "docs" / "releases" / f"{version}.md").write_text(notes)
    return umbrella


def _fake_git_run(dirty=(), fail=None, off_branch=(), gh_conclusion="success", commits=0, install_conclusion="success"):
    """`fail`, when given, is `(directory, kind)` for the one call that
    should return non-zero — everything else in a clean, on-branch tree.
    A nonzero `commits` is what `git rev-list <tag>..HEAD --count` reports for
    every component; zero keeps the non-numeric answer, which the edge reads as
    no commits since the tag.
    Every `gh run list` call reports one run with `gh_conclusion`. Tracks
    each directory's current branch (starting off-default for any directory
    named in `off_branch`) across `checkout` calls, exposed as
    `run.current_branch`. The install-proof workflow lists run 1 until
    `gh workflow run` dispatches it, then runs 2 and 1; run 2 finishes with
    `install_conclusion`."""
    calls: list = []
    current_branch = dict.fromkeys(off_branch, "feature/x")
    dispatched: list = []

    def run(argv, cwd):
        calls.append(argv)
        if argv[:3] == ["gh", "workflow", "run"]:
            dispatched.append(argv)
            return (0, "")
        if argv[:2] == ["gh", "run"] and argv[2] == "list" and "install-proof.yml" in argv:
            return (0, json.dumps([{"databaseId": i} for i in ([2, 1] if dispatched else [1])]))
        if argv[:3] == ["gh", "run", "view"]:
            return (0, json.dumps({"databaseId": 2, "status": "completed", "conclusion": install_conclusion,
                                    "url": "https://x/proof/2"}))
        if argv[0] == "gh" and argv[1] == "run":
            tag = argv[argv.index("--branch") + 1]
            return (0, json.dumps([{"status": "completed", "conclusion": gh_conclusion, "name": "ci",
                                     "url": "https://x/1", "event": "push", "headBranch": tag}]))
        if argv[0] == "gh" and argv[1] == "release" and argv[2] == "view":
            return (1, "release not found")
        if argv[0] == "gh":
            return (0, "")
        if fail and argv[2] == fail[0] and argv[3] == fail[1]:
            return (1, f"{fail[1]} failed")
        if argv[3] == "status":
            return (0, "M f\n") if argv[2] in dirty else (0, "")
        if argv[3] == "checkout":
            current_branch[argv[2]] = argv[-1]
            return (0, "")
        if argv[3] == "rev-parse":
            return (0, current_branch.get(argv[2], "main") + "\n")
        if argv[3] == "symbolic-ref":
            return (0, "refs/remotes/origin/main\n")
        if argv[3] == "rev-list":
            return _level(argv) or (0, f"{commits}\n" if commits and "--count" in argv else "deadbeef\n")
        return (0, "")
    run.current_branch = current_branch
    return calls, run


def _cut(tmp_path, monkeypatch, version=V0, *flags, fake=None, manifest_toml=_MANIFEST_TOML, notes="notes",
         index_tagged=None):
    """`cli.main(["release", version, ...])` against `_checkout`, the remotes reachable with no tags.
    `index_tagged`, when given, pre-writes the releases index as the executor would render it with those
    components tagged, so the cut at the declared version has no index to land."""
    umbrella = _checkout(tmp_path, manifest_toml, notes, version)
    if index_tagged is not None:
        rendered = release.release_index_text("", version, tomllib.loads(manifest_toml), tagged=index_tagged)
        (umbrella / "docs" / "releases" / "index.md").write_text(rendered)
    calls, run = fake or _fake_git_run()
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: [])
    monkeypatch.setattr(cli, "_real_run", run)
    monkeypatch.setattr(cli, "_component_declares_tag_trigger", lambda directory: True)
    rc = cli.main(["release", version, *flags, "--manifest", str(umbrella / "manifest.toml")])
    return rc, calls, umbrella


# ---- the plan ----


def test_first_cut_of_the_declared_version_tags_the_repository_with_no_bump():
    steps = release.release_plan(_manifest(), V0, _no_tags(_manifest()), repository_url=_REPOSITORY_URL)
    assert _kinds(steps) == ["pinned", "notes", "tag_self", "wait_workflows", "github_release", "tap_formula_pr"]
    assert steps[0] == {"kind": "pinned", "component": "dash", "tag": "v0.42.0"}
    assert steps[1] == {"kind": "notes", "component": "notes", "path": f"docs/releases/{V0}.md"}
    assert steps[2] == {"kind": "tag_self", "component": "coxswain", "tag": f"v{V0}"}
    assert steps[3] == {"kind": "wait_workflows", "component": "coxswain", "tag": f"v{V0}"}
    assert steps[4] == {"kind": "github_release", "component": "coxswain", "repo": "ppfenning/coxswain",
                        "tag": f"v{V0}", "title": f"coxswain {V0}", "notes_path": f"docs/releases/{V0}.md",
                        "heading": None}


def test_a_changed_index_at_the_declared_version_is_committed_and_landed_before_the_tag():
    steps = release.release_plan(_manifest(), V0, _no_tags(_manifest()), index_changed=True)
    kinds = _kinds(steps)
    assert kinds[kinds.index("notes") + 1:kinds.index("tag_self")] == [
        "commit_index", "push", "pr_create", "wait_checks", "merge"]
    commit = steps[kinds.index("commit_index")]
    assert commit == {"kind": "commit_index", "component": "coxswain", "branch": f"release/{V0}",
                      "commit_subject": f"release: land the {V0} releases index",
                      "paths": ["docs/releases/index.md"]}
    assert steps[kinds.index("push")]["branch"] == f"release/{V0}"
    assert steps[kinds.index("pr_create")]["body"] == release.pr_footer(V0)


def test_an_unchanged_index_at_the_declared_version_plans_no_commit():
    steps = release.release_plan(_manifest(), V0, _no_tags(_manifest()), index_changed=False)
    assert _kinds(steps) == ["pinned", "notes", "tag_self", "wait_workflows", "github_release", "tap_formula_pr"]


def test_a_bump_plan_ignores_index_changed():
    assert (release.release_plan(_manifest(), V1, _no_tags(_manifest()), index_changed=True)
            == release.release_plan(_manifest(), V1, _no_tags(_manifest())))


def test_a_later_version_bumps_pyproject_and_manifest_on_a_release_branch_and_lands_it_before_the_tag():
    steps = release.release_plan(_manifest(), V1, _no_tags(_manifest()))
    assert _kinds(steps) == ["pinned", "notes", "bump_version", "push", "pr_create", "wait_checks", "merge",
                             "tag_self", "wait_workflows", "github_release", "tap_formula_pr"]
    assert steps[2] == {"kind": "bump_version", "component": "coxswain", "from": V0, "to": V1,
                        "branch": f"release/{V1}", "commit_subject": f"release: bump to {V1} to match the tag"}
    assert steps[3] == {"kind": "push", "component": "coxswain", "branch": f"release/{V1}"}
    assert steps[4] == {"kind": "pr_create", "component": "coxswain", "title": f"release: bump to {V1} to match the tag",
                        "body": f"Bumps pyproject.toml and manifest.toml to {V1} to match tag v{V1}.\n\n"
                                + release.pr_footer(V1)}
    assert steps[5:7] == [{"kind": "wait_checks", "component": "coxswain"}, {"kind": "merge", "component": "coxswain"}]


def test_the_first_calver_release_follows_the_last_semver_one():
    steps = release.release_plan(_manifest("0.47.0"), V0, _no_tags(_manifest()))
    assert steps[2]["kind"] == "bump_version" and steps[2]["from"] == "0.47.0" and steps[2]["to"] == V0


@pytest.mark.parametrize("version", ["0.48.0", "2026.01.0", "2026.10.0-beta.1", "v2026.10.0", "not-a-version"])
def test_a_version_that_is_not_calver_refuses(version):
    steps = release.release_plan(_manifest(), version, _no_tags(_manifest()))
    assert _kinds(steps) == ["refuse"] and version in steps[0]["detail"] and "YYYY.M.N" in steps[0]["detail"]


@pytest.mark.parametrize("current, version", [(V1, V0), ("2026.10.10", "2026.10.9")])
def test_a_version_below_the_manifests_refuses(current, version):
    steps = release.release_plan(_manifest(current), version, _no_tags(_manifest()))
    assert _kinds(steps) == ["refuse"] and "not greater" in steps[0]["detail"]


def test_an_existing_tag_on_the_repository_refuses():
    steps = release.release_plan(_manifest(), V0, {**_no_tags(_manifest()), "coxswain": [f"v{V0}"]})
    assert _kinds(steps) == ["refuse"] and "coxswain" in steps[0]["detail"]


def test_an_unreadable_repository_refuses_rather_than_risk_a_reused_tag():
    for tags in ({"dash": []}, {"dash": [], "coxswain": None}):
        steps = release.release_plan(_manifest(), V0, tags)
        assert _kinds(steps) == ["refuse"] and "unknown" in steps[0]["detail"]


def test_a_pinned_dashs_unreadable_remote_or_colliding_tag_does_not_refuse_the_release():
    for dash_tags in (None, [f"v{V0}"]):
        steps = release.release_plan(_manifest(), V0, {"coxswain": [], "dash": dash_tags})
        assert _kinds(steps)[0] == "pinned" and "refuse" not in _kinds(steps)


def test_a_changed_dash_is_tagged_at_the_release_version_with_its_own_github_release():
    steps = release.release_plan(_manifest(), V0, _no_tags(_manifest()), pinned_commits={"dash": 2},
                                 repository_url=_REPOSITORY_URL)
    assert _kinds(steps)[:4] == ["tag", "wait_workflows", "github_release", "notes"]
    assert steps[0] == {"kind": "tag", "component": "dash", "repo": "org/dash", "tag": f"v{V0}"}
    assert steps[2] == {"kind": "github_release", "component": "dash", "repo": "org/dash", "tag": f"v{V0}",
                        "title": f"coxswain-dash {V0}", "notes_path": f"docs/releases/{V0}.md",
                        "heading": "## coxswain-dash", "from": "v0.42.0",
                        "link": f"https://github.com/ppfenning/coxswain/releases/tag/v{V0}"}
    assert release.rejoined(steps) == {"dash"}


def test_a_changed_dash_behind_the_version_gets_bump_cargo_and_the_land_sequence_before_its_tag():
    steps = release.release_plan(_manifest(), V0, _no_tags(_manifest()), pinned_commits={"dash": 1},
                                 cargo_versions={"dash": "0.42.0"})
    dash = [s for s in steps if s["component"] == "dash"]
    assert _kinds(dash) == ["bump_cargo", "push", "pr_create", "wait_checks", "merge", "tag", "wait_workflows",
                            "github_release"]
    assert dash[0] == {"kind": "bump_cargo", "component": "dash", "repo": "org/dash", "branch": f"release/{V0}",
                       "commit_subject": f"cargo: bump to {V0} to match the tag", "from": "0.42.0", "to": V0}
    assert dash[2]["body"] == (f"Bumps dash's Cargo.toml version to {V0} to match tag v{V0}.\n\n"
                               + release.pr_footer(V0))


def test_a_changed_dash_already_at_the_version_gets_no_bump():
    steps = release.release_plan(_manifest(), V0, _no_tags(_manifest()), pinned_commits={"dash": 1},
                                 cargo_versions={"dash": V0})
    assert _kinds([s for s in steps if s["component"] == "dash"]) == ["tag", "wait_workflows", "github_release"]


def test_a_dash_joining_at_its_since_version_is_tagged_with_no_commit_count():
    steps = release.release_plan(_manifest(since=V0), V0, _no_tags(_manifest()))
    assert {"kind": "tag", "component": "dash", "repo": "org/dash", "tag": f"v{V0}"} in steps


def test_a_changed_dashs_existing_tag_refuses():
    steps = release.release_plan(_manifest(), V0, {"coxswain": [], "dash": [f"v{V0}"]}, pinned_commits={"dash": 1})
    assert _kinds(steps) == ["refuse"] and "dash" in steps[0]["detail"]


def test_wait_workflows_follows_every_tag_and_nothing_else():
    steps = release.release_plan(_manifest(), V1, _no_tags(_manifest()), pinned_commits={"dash": 1},
                                 cargo_versions={"dash": "0.42.0"})
    kinds = _kinds(steps)
    for i, kind in enumerate(kinds[:-1]):
        assert (kinds[i + 1] == "wait_workflows") == (kind in ("tag", "tag_self"))


def test_every_release_ends_with_the_tap_pr_for_the_coxswain_sdist():
    step = release.release_plan(_manifest(), V0, _no_tags(_manifest()))[-1]
    assert step == {"kind": "tap_formula_pr", "component": "tap", "repo": "ppfenning/homebrew-coxswain",
                    "path": "Formula/cox.rb", "version": V0, "branch": f"release/{V0}", "title": f"cox {V0}",
                    "index_url": f"https://pypi.org/pypi/coxswain/{V0}/json", "towpath_version": "0.42.0"}


def test_the_tap_pr_points_towpath_at_dash_when_the_release_tags_it():
    step = release.release_plan(_manifest(), V0, _no_tags(_manifest()), pinned_commits={"dash": 1})[-1]
    assert step["towpath_version"] == V0


def test_a_dirty_or_absent_tap_checkout_refuses_naming_the_tap():
    for state in ("dirty", "absent"):
        steps = release.release_plan(_manifest(), V0, _no_tags(_manifest()), tap_state=state)
        assert steps[-1]["kind"] == "refuse" and steps[-1]["component"] == "tap" and state in steps[-1]["detail"]


def test_tap_only_returns_just_the_tap_step_once_the_tag_exists():
    tags = {"coxswain": [f"v{V0}"], "dash": []}
    assert _kinds(release.release_plan(_manifest(), V0, tags, tap_only=True)) == ["tap_formula_pr"]


def test_tap_only_refuses_without_the_tag_or_with_an_unreadable_repository():
    missing = release.release_plan(_manifest(), V0, {"coxswain": [], "dash": []}, tap_only=True)
    assert _kinds(missing) == ["refuse"] and "missing on coxswain" in missing[0]["detail"]
    unknown = release.release_plan(_manifest(), V0, {"dash": []}, tap_only=True)
    assert _kinds(unknown) == ["refuse"] and "unknown" in unknown[0]["detail"]


def test_tap_only_needs_the_tag_on_a_dash_the_release_tagged():
    steps = release.release_plan(_manifest(), V0, {"coxswain": [f"v{V0}"], "dash": []}, pinned_commits={"dash": 1},
                                 tap_only=True)
    assert _kinds(steps) == ["refuse"] and "dash" in steps[0]["detail"]


# ---- the drift gate ----


def test_gate_refuses_one_step_per_drift_when_no_reason_is_given():
    drifts = [Drift("cli-surface", "docs/reference/cli/x.md", None, "cli.py", 42, "add cox x to the docs")]
    assert release.gate(drifts, None) == [
        {"kind": "refuse", "component": "cli-surface",
         "detail": "cli-surface: docs/reference/cli/x.md <-> cli.py:42 — add cox x to the docs"}
    ]


def test_gate_notes_the_reason_and_drift_count_when_one_is_given():
    drifts = [Drift("cli-surface", "a", None, "b", None, "fix a"), Drift("manifest", "a", None, "b", None, "fix b")]
    assert release.gate(drifts, "docs land next sprint") == [
        {"kind": "note", "component": "release-check", "detail": "docs land next sprint (2 drifts allowed)"}
    ]


def test_gate_is_a_no_op_with_no_drifts():
    assert release.gate([], None) == []
    assert release.gate([], "any reason") == []


def test_gate_refuses_a_versions_drift_even_when_a_reason_is_given():
    drifts = [Drift("versions", "manifest.toml", None, "dash/Cargo.toml", None,
                     f"dash Cargo.toml is 0.42.0, manifest wants {V0}")]
    assert release.gate(drifts, "docs land next sprint") == [
        {"kind": "refuse", "component": "versions",
         "detail": f"versions: manifest.toml <-> dash/Cargo.toml — dash Cargo.toml is 0.42.0, "
                   f"manifest wants {V0} — bump it by hand"}
    ]


def test_gate_a_behind_cargo_toml_with_a_planned_bump_cargo_passes_the_gate():
    drifts = [Drift("versions", "manifest.toml", None, "dash/Cargo.toml", None,
                     f"dash Cargo.toml is 0.42.0, manifest wants {V0} (devtools release {V0} performs the bump)")]
    plan_steps = [{"kind": "bump_cargo", "component": "dash", "to": V0}]
    assert release.gate(drifts, None, plan_steps) == [
        {"kind": "note", "component": "versions",
         "detail": f"versions: manifest.toml <-> dash/Cargo.toml — dash Cargo.toml is 0.42.0, "
                   f"manifest wants {V0} — the release bumps it"}
    ]
    assert release.gate(drifts, None, [])[0]["kind"] == "refuse"


def test_gate_a_pyproject_behind_the_manifest_with_a_planned_bump_version_passes():
    drifts = [Drift("versions", "manifest.toml", None, "coxswain/pyproject.toml", None,
                     f"umbrella pyproject.toml is {V0}, manifest wants {V1} (devtools release {V1} performs the bump)")]
    plan_steps = [{"kind": "bump_version", "component": "coxswain", "from": V0, "to": V1}]
    assert release.gate(drifts, None, plan_steps)[0]["kind"] == "note"


# ---- pure helpers ----


def test_bumped_manifest_text_rewrites_the_version_and_a_tagged_inline_component_and_no_other_byte():
    text = Path(__file__).resolve().parents[2].joinpath("manifest.toml").read_text()
    after_text = release.bumped_manifest_text(text, V1, rejoining={"dash"})
    before, after = tomllib.loads(text), tomllib.loads(after_text)
    assert after["coxswain"]["version"] == V1 and after["components"]["dash"]["tag"] == f"v{V1}"
    before["coxswain"]["version"], before["components"]["dash"]["tag"] = V1, f"v{V1}"
    assert before == after
    assert after_text.count("\n") == text.count("\n")


def test_bumped_manifest_text_leaves_a_pinned_component_and_other_tables_alone():
    text = ('# header: version = "x"\n[coxswain]\nversion = "2026.10.0"  # the released version\n\n'
            '[components]\ndash = { repo = "org/dash", tag = "v0.42.0", docs = [\n    "README.md",\n] }\n\n'
            '[notify]\nslack = { channel = "#releases", tag = "v9.9.9" }\nversion = "keep"\n')
    after_text = release.bumped_manifest_text(text, V1)
    assert after_text == text.replace('version = "2026.10.0"', f'version = "{V1}"')
    assert release.bumped_manifest_text(text, V1, rejoining={"slack"}) == after_text


def test_bumped_manifest_text_rewrites_a_section_style_component_named_as_tagged():
    text = '[coxswain]\nversion = "2026.10.0"\n\n[components.dash]\nrepo = "org/dash"\ntag = "v0.42.0"\n'
    assert release.bumped_manifest_text(text, V1, rejoining={"dash"}) == text.replace(
        '"2026.10.0"', f'"{V1}"').replace('"v0.42.0"', f'"v{V1}"')


def test__inline_owners_carries_a_components_name_across_its_lines_and_stops_where_the_braces_balance():
    lines = ['[components]', 'a = { x = [', '  "y",', '] }', 'b = { c = 1 }', '[other]', 'd = { e = 1 }']
    assert release._inline_owners(lines) == [None, "a", "a", "a", "b", None, None]


def test_bumped_version_line_and_lock_stanzas_rewrite_only_their_own_package():
    assert release.bumped_version_line('[project]\nname = "coxswain"\nversion = "2026.10.0"\n', V1) == (
        f'[project]\nname = "coxswain"\nversion = "{V1}"\n')
    lock = 'version = 1\n\n[[package]]\nname = "colorama"\nversion = "0.4.6"\n\n[[package]]\nname = "coxswain"\nversion = "2026.10.0"\n'
    assert release.bumped_uv_lock_text(lock, "coxswain", V1) == lock.replace('"2026.10.0"', f'"{V1}"')
    cargo_lock = 'version = 3\n\n[[package]]\nname = "towpath"\nversion = "0.42.0"\n\n[[package]]\nname = "other"\nversion = "0.5.0"\n'
    assert release.bumped_cargo_lock_text(cargo_lock, "towpath", V0) == cargo_lock.replace('"0.42.0"', f'"{V0}"')


_FORMULA = ('class Cox < Formula\n  url "https://x/coxswain_tools-0.47.0.tar.gz"\n  sha256 "' + "0" * 64 + '"\n'
            '  head "https://github.com/ppfenning/coxswain-tools.git", branch: "main"\n\n'
            '  resource "dep" do\n    url "https://x/dep-1.0.tar.gz"\n    sha256 "' + "a" * 64 + '"\n  end\nend\n')

# A formula with every anchor `bumped_formula_text(..., towpath=...)` needs: a resource, `def install` and `test do`.
_TAP_FORMULA = (_FORMULA.removesuffix("end\n")
                + '\n  def install\n    virtualenv_install_with_resources\n  end\n\n'
                + '  test do\n    system bin/"cox", "--version"\n  end\nend\n')


def _dash_text(url):
    """Every `.sha256` dash asset reads as one digest line."""
    return "c" * 64 + "  " + url.rsplit("/", 1)[-1].removesuffix(".sha256") + "\n"


def test_bumped_formula_text_moves_url_sha_and_head_to_coxswain_and_leaves_the_resource_stanza():
    after = release.bumped_formula_text(_FORMULA, V0, f"https://x/coxswain-{V0}.tar.gz", "b" * 64)
    assert after == (_FORMULA.replace("coxswain_tools-0.47.0", f"coxswain-{V0}").replace("0" * 64, "b" * 64)
                     .replace("ppfenning/coxswain-tools.git", "ppfenning/coxswain.git"))


def test_bumped_formula_text_returns_unrecognised_text_verbatim():
    assert release.bumped_formula_text("not a formula\n", V0, "u", "s") == "not a formula\n"


def test_sdist_from_index_picks_the_sdist_url_and_sha256():
    payload = {"urls": [{"packagetype": "bdist_wheel", "url": "w", "digests": {"sha256": "1"}},
                        {"packagetype": "sdist", "url": "s", "digests": {"sha256": "2"}}]}
    assert release.sdist_from_index(payload) == ("s", "2")
    assert release.sdist_from_index({"urls": []}) is None


def test_umbrella_release_slug_prefers_the_manifest_then_the_repository_url_then_the_default():
    assert release.umbrella_release_slug({"coxswain": {"repo": "org/coxswain"}}) == "org/coxswain"
    assert release.umbrella_release_slug({}, "https://github.com/org/fork.git") == "org/fork"
    assert release.umbrella_release_slug({}) == "ppfenning/coxswain"


def test_is_maintainer_remote_names_the_coxswain_repository_not_its_siblings():
    assert release.is_maintainer_remote("git@github.com:ppfenning/coxswain.git")
    assert release.is_maintainer_remote("https://github.com/ppfenning/coxswain")
    assert not release.is_maintainer_remote("https://github.com/ppfenning/coxswain-dash.git")
    assert not release.is_maintainer_remote("https://github.com/someone/coxswain")


def test_versions_oldest_first_puts_the_semver_line_below_calver():
    assert release.versions_oldest_first(["2026.10.10", "0.47.0", "2026.11.0", "index", "2026.10.9", "0.9.0"]) == [
        "0.9.0", "0.47.0", "2026.10.9", "2026.10.10", "2026.11.0", "index"]


def test_declares_tag_trigger_reads_the_dict_form_and_skips_the_list_form():
    assert release.declares_tag_trigger('on:\n  push:\n    tags: ["v*"]\n') is True
    assert release.declares_tag_trigger("on: [push, pull_request]\n") is False


def test_parse_ls_remote_is_pure_and_skips_peeled_refs():
    text = "aaa\trefs/tags/v0.47.0\nbbb\trefs/tags/v0.47.0^{}\nccc\trefs/heads/main\nddd\trefs/tags/v2026.10.0\n"
    assert release.parse_ls_remote(text) == ["v0.47.0", "v2026.10.0"]


def test_component_dir_tag_argv_and_push_argv_shape(tmp_path):
    assert release.component_dir("/root", "dash") == "/root/dash"
    assert release.component_dir("/root", "dash", {"dash": "/dev/dash"}) == "/dev/dash"
    (tmp_path / "coxswain-dash").mkdir()
    assert release.component_dir(str(tmp_path), "dash") == str(tmp_path / "coxswain-dash")
    assert release.tag_argv("/c", V0) == ["git", "-C", "/c", "tag", "-a", f"v{V0}", "-m", f"coxswain {V0}"]
    assert release.push_argv("/c", V0) == ["git", "-C", "/c", "push", "origin", f"v{V0}"]


def test_component_version_reads_the_project_table_version():
    assert release.component_version('[project]\nname = "x"\nversion = "2026.10.0"\n') == V0
    assert release.component_version('[project]\nname = "x"\n') is None


def test_release_index_text_lists_a_tagged_component_at_the_release_and_a_pinned_one_at_its_old_tag():
    manifest = {"components": {"dash": {"repo": "org/dash", "tag": "v0.42.0"}}}
    assert release.release_index_text("", V0, manifest) == release_check_index.index_section(
        V0, {"dash": "v0.42.0"}, manifest["components"]) + "\n"
    assert release.release_index_text("", V0, manifest, tagged={"dash"}) == release_check_index.index_section(
        V0, {"dash": f"v{V0}"}, manifest["components"]) + "\n"


def test_release_index_text_is_unchanged_when_the_versions_section_is_already_present():
    manifest = {"components": {"dash": {"repo": "org/dash", "tag": "v0.42.0"}}}
    existing = release.release_index_text("", V0, manifest)
    assert release.release_index_text(existing, V0, manifest) == existing


def test_release_index_text_puts_a_calver_release_above_every_semver_one_below_the_preamble():
    manifest = {"components": {"dash": {"repo": "org/dash", "tag": "v0.42.0"}}}
    existing = "# Releases\n\n## How a release works\n\ntext"
    for version in ("0.47.0", V1, V0, "0.9.0"):
        existing = release.release_index_text(existing, version, manifest)
    order = [existing.index(h) for h in ("## How", f"## `{V1}`", f"## `{V0}`", "## `0.47.0`", "## `0.9.0`")]
    assert order == sorted(order)


def test_release_index_text_does_not_drop_an_adjacent_section_missing_its_blank_line():
    manifest = {"components": {"dash": {"repo": "org/dash", "tag": "v0.42.0"}}}
    hand_written = f"## `{V0}`\n\n| dash | x | `v0.42.0` | flag |\n## `0.47.0`\n\n| tools | x | `v0.47.0` | required |\n"
    text = release.release_index_text(hand_written, V0, manifest)
    assert text.count(f"## `{V0}`") == 1
    assert "## `0.47.0`\n\n| tools | x | `v0.47.0` | required |" in text


def test_extract_release_notes_returns_the_section_up_to_the_next_heading(tmp_path):
    notes = tmp_path / f"{V0}.md"
    notes.write_text(f"# {V0}\n\n## coxswain-dash\ndash notes\n\n## Known gaps\ngaps\n")
    assert release.extract_release_notes(str(notes), "## coxswain-dash") == "## coxswain-dash\ndash notes\n\n"
    assert release.extract_release_notes(str(notes), "## coxswain-crew") is None
    assert release.extract_release_notes(str(tmp_path / "missing.md"), "## coxswain-dash") is None


def test_github_release_argv_shapes():
    assert release.github_release_view_argv(f"v{V0}") == ["gh", "release", "view", f"v{V0}"]
    assert release.github_release_create_argv(f"v{V0}", f"coxswain {V0}", f"docs/releases/{V0}.md") == [
        "gh", "release", "create", f"v{V0}", "--verify-tag", "--title", f"coxswain {V0}",
        "--notes-file", f"docs/releases/{V0}.md"]
    assert release.github_release_edit_argv(f"v{V0}", f"docs/releases/{V0}.md") == [
        "gh", "release", "edit", f"v{V0}", "--notes-file", f"docs/releases/{V0}.md"]


@pytest.mark.parametrize("conclusion, may", [("success", True), ("failure", False), ("cancelled", False),
                                             ("skipped", False), ("timed_out", False), (None, False)])
def test_tap_may_merge_only_on_success(conclusion, may):
    assert release.tap_may_merge(conclusion) is may


def test_install_proof_argv_and_parsing_are_literal():
    assert release.install_proof_dispatch_argv(f"release/{V0}", V0) == [
        "gh", "workflow", "run", "install-proof.yml", "-f", f"tap_ref=release/{V0}", "-f", f"version={V0}"]
    assert release.install_proof_ids('[{"databaseId": 7}, {"databaseId": 5}]') == [7, 5]
    assert release.install_proof_ids("[]") == []
    assert release.install_proof_ids("not json") is None
    assert release.install_proof_ids('[{"id": 7}]') is None
    assert release.new_install_proof_runs([5], [7, 5, 3]) == [7]
    assert release.new_install_proof_runs([], [7]) == [7]
    assert release.install_proof_result('{"conclusion": "failure", "url": "u"}') == ("failure", "u")
    assert release.install_proof_result('{"conclusion": ""}') == (None, None)
    assert release.install_proof_result("not json") == (None, None)


def test_repository_url_reads_installed_package_metadata_not_a_checkout_path(monkeypatch):
    """Proves the lookup survives being installed (editable counts), unlike a
    `pyproject.toml` path that only exists in a checkout."""
    monkeypatch.undo()
    assert cli._repository_url() == _REPOSITORY_URL


# ---- the edge: remotes, the tap, the workflows ----


class _LsRemote:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout = returncode, stdout


def test_remote_tags_parses_ls_remote_output_and_is_none_on_a_failing_git(monkeypatch):
    stdout = "abc\trefs/tags/v0.47.0\ndef\trefs/tags/v0.47.0^{}\nghi\trefs/tags/v2026.10.0\n"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _LsRemote(0, stdout))
    assert cli._remote_tags("ppfenning/coxswain") == ["v0.47.0", "v2026.10.0"]
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _LsRemote(128, ""))
    assert cli._remote_tags("org/nope") is None


def test_tap_state_reads_absent_clean_and_dirty(tmp_path, monkeypatch):
    monkeypatch.undo()
    assert cli._tap_state(str(tmp_path / "missing")) == "absent"
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    assert cli._tap_state(str(tmp_path)) == "clean"
    (tmp_path / "f").write_text("x")
    assert cli._tap_state(str(tmp_path)) == "dirty"


def _tap_step(version=V0):
    return release.release_plan(_manifest(), version, _no_tags(_manifest()))[-1]


def _run_tap_step(tmp_path, install_conclusion="success", override=None, version=V0):
    """`override(argv)` returns a `(rc, out)` that replaces the fake runner's answer, or None to keep it."""
    directory = tmp_path / "homebrew-coxswain"
    formula = directory / "Formula" / "cox.rb"
    formula.parent.mkdir(parents=True, exist_ok=True)
    formula.write_text(_TAP_FORMULA)
    calls, inner = _fake_git_run(install_conclusion=install_conclusion)
    clock = iter(range(0, 10**9, 60))

    def fake_run(argv, cwd):
        replaced = override(argv) if override else None
        if replaced is None:
            return inner(argv, cwd)
        calls.append(argv)
        return replaced

    index = {"urls": [{"packagetype": "sdist", "url": f"https://x/coxswain-{version}.tar.gz", "digests": {"sha256": "b" * 64}}]}
    rc = cli._release_execute([_tap_step(version)], version, str(tmp_path), {}, str(tmp_path), fake_run, {}, "",
                              sleep=lambda s: None, now=lambda: next(clock), fetch_index=lambda url: index,
                              fetch_text=_dash_text)
    return rc, calls, formula, inner


def test_cli_release_execute_opens_the_tap_pr_with_the_index_sdist_and_the_coxswain_head(tmp_path):
    rc, calls, formula, run = _run_tap_step(tmp_path)
    assert rc == 0
    text = formula.read_text()
    assert f'url "https://x/coxswain-{V0}.tar.gz"' in text and "b" * 64 in text
    assert 'head "https://github.com/ppfenning/coxswain.git", branch: "main"' in text
    assert ["gh", "pr", "create", "--title", f"cox {V0}", "--body",
            f"Bumps the formula to {V0} on PyPI.\n\n" + release.pr_footer(V0)] in calls
    assert run.current_branch[str(tmp_path / "homebrew-coxswain")] == "main"


def test_cli_release_execute_branches_the_tap_pr_from_the_fetched_origin_default_not_the_local_head(tmp_path):
    directory = tmp_path / "homebrew-coxswain"
    fetched = []

    def override(argv):
        if argv[0] == "git" and argv[3] == "fetch":
            fetched.append(argv[2])
            return (0, "")
        if argv[0] == "git" and argv[3] == "symbolic-ref":
            return (0, "refs/remotes/origin/master\n") if argv[2] in fetched else (1, "not a symbolic ref")
        return None

    rc, calls, _, _ = _run_tap_step(tmp_path, override=override)
    branch_from_fetched = release.checkout_branch_argv(str(directory), f"release/{V0}", "origin/master")
    assert rc == 0
    assert calls.index(release.fetch_argv(str(directory))) < calls.index(branch_from_fetched)
    assert [c for c in calls if c[3:5] == ["checkout", "-b"]] == [branch_from_fetched]


def test_cli_release_execute_refuses_the_tap_pr_when_the_fetch_fails(tmp_path, capsys):
    rc, calls, formula, _ = _run_tap_step(tmp_path, override=lambda argv: (1, "fetch failed") if argv[3:4] == ["fetch"] else None)
    assert rc == 2
    assert "FAILED tap_formula_pr tap: fetch failed" in capsys.readouterr().out
    assert [c for c in calls if c[0] == "gh" or c[3] in ("checkout", "add", "commit", "push")] == []
    assert formula.read_text() == _TAP_FORMULA


def _merged(calls):
    return release.pr_merge_argv() in calls


def test_cli_release_execute_refuses_without_dispatch_when_the_baseline_run_list_fails(tmp_path, capsys):
    rc, calls, _, _ = _run_tap_step(tmp_path, override=lambda argv: (1, "HTTP 502") if argv[2:3] == ["list"] else None)
    assert rc == 2
    assert not _merged(calls)
    assert [c for c in calls if c[:3] == ["gh", "workflow", "run"]] == []
    assert "existing runs could not be listed" in capsys.readouterr().out


def test_cli_release_execute_refuses_when_no_new_run_appears_and_only_a_stale_success_is_listed(tmp_path, capsys):
    stale = (0, json.dumps([{"databaseId": 1}]))
    rc, calls, _, _ = _run_tap_step(tmp_path, override=lambda argv: stale if argv[2:3] == ["list"] else None)
    assert rc == 2
    assert not _merged(calls)
    assert "did not appear after dispatch" in capsys.readouterr().out


def test_cli_release_execute_refuses_when_another_run_starts_beside_the_dispatched_one(tmp_path, capsys):
    lists = iter([[1], [2, 1], [3, 2, 1]])
    rc, calls, _, _ = _run_tap_step(tmp_path, override=lambda argv: (
        (0, json.dumps([{"databaseId": i} for i in next(lists)])) if argv[2:3] == ["list"] else None))
    assert rc == 2
    assert "ambiguous" in capsys.readouterr().out


def test_cli_release_execute_refuses_when_the_dispatch_fails(tmp_path, capsys):
    rc, _, _, _ = _run_tap_step(tmp_path, override=lambda argv: (1, "no such workflow") if argv[1:2] == ["workflow"] else None)
    assert rc == 2
    assert "no such workflow" in capsys.readouterr().out


def test_cli_release_execute_refuses_when_the_proof_never_finishes(tmp_path, capsys):
    running = (0, json.dumps({"databaseId": 2, "status": "in_progress", "conclusion": ""}))
    rc, _, _, _ = _run_tap_step(tmp_path, override=lambda argv: running if argv[2:3] == ["view"] else None)
    assert rc == 2
    assert "did not finish" in capsys.readouterr().out


def test_cli_release_execute_leaves_the_tap_pr_open_when_the_install_proof_fails(tmp_path, capsys):
    rc, calls, _, _ = _run_tap_step(tmp_path, install_conclusion="failure")
    assert rc != 0
    assert not _merged(calls)
    assert release.install_proof_dispatch_argv(f"release/{V0}", V0) in calls
    out = capsys.readouterr().out
    assert "failure" in out and "https://x/proof/2" in out


def test_cli_release_execute_succeeds_after_a_passing_proof_and_issues_no_merge(tmp_path):
    dispatch = release.install_proof_dispatch_argv(f"release/{V0}", V0)
    rc, calls, _, _ = _run_tap_step(tmp_path)
    assert rc == 0
    assert calls.index(dispatch) < calls.index(release.install_proof_view_argv(2))
    assert not _merged(calls)


def test_wait_workflows_proceeds_when_every_push_run_on_the_tag_succeeds(monkeypatch):
    monkeypatch.setattr(cli, "_component_declares_tag_trigger", lambda directory: True)
    calls, run = _fake_git_run()
    assert cli._wait_workflows("/c", f"v{V0}", "coxswain", run) == (True, f"1 run(s) green for v{V0}")


def test_wait_workflows_fails_naming_the_workflow_and_url_when_one_run_is_not_success(monkeypatch):
    monkeypatch.setattr(cli, "_component_declares_tag_trigger", lambda directory: True)
    _, run = _fake_git_run(gh_conclusion="failure")
    assert cli._wait_workflows("/c", f"v{V0}", "coxswain", run) == (
        False, "coxswain: ci did not succeed (https://x/1)")


def test_wait_workflows_retries_until_a_pending_run_concludes(monkeypatch):
    monkeypatch.setattr(cli, "_component_declares_tag_trigger", lambda directory: True)
    answers = iter([[], [{"status": "queued", "event": "push", "headBranch": "v1"}],
                    [{"status": "completed", "conclusion": "success", "event": "push", "headBranch": "v1"}]])
    slept = []
    ok, _ = cli._wait_workflows("/c", "v1", "coxswain", lambda argv, cwd: (0, json.dumps(next(answers))),
                                sleep=slept.append, now=lambda: 0.0)
    assert ok and slept == [10, 10]


def test_wait_workflows_returns_at_once_for_a_checkout_with_no_tag_trigger_and_fails_on_zero_runs_with_one(tmp_path):
    assert cli._wait_workflows(str(tmp_path), "v1", "dash", lambda argv, cwd: pytest.fail("polled"))[0] is True
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "publish.yml").write_text('on:\n  push:\n    tags: ["v*"]\n')
    clock = iter([0, 0, 1000])
    ok, detail = cli._wait_workflows(str(tmp_path), "v1", "coxswain", lambda argv, cwd: (0, "[]"),
                                     timeout_s=900, sleep=lambda s: None, now=lambda: next(clock))
    assert not ok and "no workflow run started for v1" in detail


def _wait_checks_run(answers):
    calls = []

    def run(argv, cwd):
        calls.append(argv)
        return next(answers)
    return calls, run


def test_release_execute_wait_checks_polls_until_green(tmp_path, capsys):
    calls, run = _wait_checks_run(iter([(1, f"no checks reported on the 'release/{V1}' branch")] * 2 + [(0, "")]))
    slept = []
    rc = cli._release_execute([{"kind": "wait_checks", "component": "coxswain"}], V1, str(tmp_path), {}, str(tmp_path),
                              run, {"components": {}}, str(tmp_path / "manifest.toml"), sleep=slept.append, now=lambda: 0.0)
    assert rc == 0
    assert calls == [release.pr_checks_argv()] * 3 and slept == [15, 15]
    assert "wait_checks coxswain: green" in capsys.readouterr().out


def test_release_execute_wait_checks_times_out_past_grace(tmp_path, capsys):
    calls, run = _wait_checks_run(iter([(1, f"no checks reported on the 'release/{V1}' branch")] * 100))
    clock = [0.0]
    rc = cli._release_execute([{"kind": "wait_checks", "component": "coxswain"}], V1, str(tmp_path), {}, str(tmp_path),
                              run, {"components": {}}, str(tmp_path / "manifest.toml"),
                              sleep=lambda s: clock.__setitem__(0, clock[0] + s), now=lambda: clock[0])
    assert rc == 2
    assert "FAILED wait_checks coxswain: no checks reported within 180s" in capsys.readouterr().out


# ---- the edge: a whole cut ----


def test_cli_release_dry_run_prints_every_step_and_touches_no_file(tmp_path, capsys, monkeypatch):
    rc, calls, umbrella = _cut(tmp_path, monkeypatch, V1, "--dry-run")
    out = capsys.readouterr().out
    assert rc == 0
    for line in ("pinned dash: v0.42.0", f"notes notes: docs/releases/{V1}.md",
                 f"bump_version coxswain: pyproject.toml, uv.lock, manifest.toml {V0} -> {V1} on release/{V1}",
                 f"tag_self coxswain: v{V1}",
                 f"github_release coxswain: ppfenning/coxswain -> gh release create v{V1} --verify-tag "
                 f"--title coxswain {V1} --notes-file docs/releases/{V1}.md",
                 f"tap_formula_pr tap: ppfenning/homebrew-coxswain Formula/cox.rb from https://pypi.org/pypi/coxswain/{V1}/json",
                 f"would run install-proof.yml with tap_ref=release/{V1} version={V1}"):
        assert line in out
    assert (umbrella / "manifest.toml").read_text() == _MANIFEST_TOML
    assert [c for c in calls if c[0] == "gh" or c[3] in ("tag", "push", "checkout", "commit")] == []


def test_cli_release_tags_and_pushes_the_checkout_then_writes_its_github_release(tmp_path, monkeypatch, no_tap):
    rc, calls, umbrella = _cut(tmp_path, monkeypatch, index_tagged=set())
    assert rc == 0
    assert [c for c in calls if c[3] in ("tag", "push")] == [release.tag_argv(str(umbrella), V0),
                                                           release.push_argv(str(umbrella), V0)]
    assert [c for c in calls if c[:2] == ["gh", "run"]] == [
        ["gh", "run", "list", "--branch", f"v{V0}", "--json", "status,conclusion,name,url,event,headBranch"]]
    assert [(c[2], c[3]) for c in calls if c[:2] == ["gh", "release"]] == [("view", f"v{V0}"), ("create", f"v{V0}")]


def test_cli_release_tags_a_changed_dash_before_the_checkout(tmp_path, monkeypatch, no_tap, capsys):
    rc, calls, umbrella = _cut(tmp_path, monkeypatch, fake=_fake_git_run(commits=1), index_tagged={"dash"})
    assert rc == 0
    dash = str(tmp_path / "dash")
    assert [c for c in calls if c[3] in ("tag", "push")] == [
        release.tag_argv(dash, V0), release.push_argv(dash, V0),
        release.tag_argv(str(umbrella), V0), release.push_argv(str(umbrella), V0)]
    assert "tag dash: v0.42.0" not in capsys.readouterr().out


def test_cli_release_appends_the_index_section_and_a_second_run_leaves_it_unchanged(tmp_path, monkeypatch, no_tap):
    umbrella = _checkout(tmp_path)
    index_path = umbrella / "docs" / "releases" / "index.md"
    index_path.write_text("## `0.47.0`\n\n| tools | x | `v0.47.0` | required |\n")
    rc, _, _ = _cut(tmp_path, monkeypatch)
    assert rc == 0
    after_first = index_path.read_text()
    assert after_first.index(f"## `{V0}`") < after_first.index("## `0.47.0`") and "| dash |" in after_first
    rc2, _, _ = _cut(tmp_path, monkeypatch)
    assert rc2 == 0 and index_path.read_text() == after_first


def test_cli_release_stops_when_the_publish_workflow_fails_and_writes_no_github_release(tmp_path, monkeypatch, no_tap, capsys):
    rc, calls, _ = _cut(tmp_path, monkeypatch, fake=_fake_git_run(gh_conclusion="failure"))
    assert rc == 2
    assert "FAILED wait_workflows coxswain: coxswain: ci did not succeed (https://x/1)" in capsys.readouterr().out
    assert [c for c in calls if c[:2] == ["gh", "release"]] == []


def test_cli_release_runs_the_bump_version_land_sequence_and_tags_what_merged(tmp_path, monkeypatch, no_tap):
    umbrella = _checkout(tmp_path, notes="notes", version=V1)
    (umbrella / "pyproject.toml").write_text(f'[project]\nname = "coxswain"\nversion = "{V0}"\n')
    (umbrella / "uv.lock").write_text(
        f'version = 1\n\n[[package]]\nname = "colorama"\nversion = "0.4.6"\n\n[[package]]\nname = "coxswain"\nversion = "{V0}"\n')
    rc, calls, _ = _cut(tmp_path, monkeypatch, V1)
    assert rc == 0
    # The fetch and the sync gate's `rev-list --count` are not part of the bump-and-land sequence under test.
    own = [c for c in calls if c[0] == "git" and c[2] == str(umbrella) and c[3] != "fetch" and "--count" not in c]
    # After `merge` the executor switches back to the default branch and pulls it before `tag_self` tags HEAD,
    # so the tag lands on the squash-merged commit rather than the stale one on release/<version>.
    assert [c[3] for c in own] == ["status", "rev-parse", "symbolic-ref", "checkout", "add", "commit", "push", "status",
                                   "symbolic-ref", "checkout", "pull", "tag", "push"]
    assert own[3] == release.checkout_branch_argv(str(umbrella), f"release/{V1}")
    assert own[4] == release.add_argv(str(umbrella), "manifest.toml", "docs/releases/index.md", "pyproject.toml", "uv.lock")
    assert own[9:11] == [release.checkout_ref_argv(str(umbrella), "main"), release.pull_argv(str(umbrella), "main")]
    assert ["gh", "pr", "merge", "--squash", "--delete-branch"] in calls
    assert f'version = "{V1}"' in (umbrella / "manifest.toml").read_text()
    assert f'version = "{V1}"' in (umbrella / "pyproject.toml").read_text()
    assert f'name = "coxswain"\nversion = "{V1}"' in (umbrella / "uv.lock").read_text()
    assert 'name = "colorama"\nversion = "0.4.6"' in (umbrella / "uv.lock").read_text()


def test_release_execute_bump_version_leaves_the_files_untouched_when_checkout_fails(tmp_path):
    umbrella = _checkout(tmp_path)
    (umbrella / "pyproject.toml").write_text(f'[project]\nname = "coxswain"\nversion = "{V0}"\n')
    calls, fake_run = _fake_git_run(fail=(str(umbrella), "checkout"))
    step = release.release_plan(_manifest(), V1, _no_tags(_manifest()))[2]
    rc = cli._release_execute([step], V1, str(tmp_path), {}, str(umbrella), fake_run, {"components": {}},
                              str(umbrella / "manifest.toml"))
    assert rc == 2
    assert (umbrella / "manifest.toml").read_text() == _MANIFEST_TOML
    assert not any(c[0] == "git" and c[3] in ("add", "commit") for c in calls)


def test_release_execute_bump_cargo_rewrites_cargo_toml_and_its_own_cargo_lock_stanza_in_one_commit(tmp_path, capsys):
    dash = tmp_path / "dash"
    dash.mkdir()
    (dash / "Cargo.toml").write_text('[package]\nname = "towpath"\nversion = "0.42.0"\nedition = "2021"\n')
    (dash / "Cargo.lock").write_text(
        'version = 3\n\n[[package]]\nname = "towpath"\nversion = "0.42.0"\n\n[[package]]\nname = "other"\nversion = "0.1.0"\n')
    step = release.release_plan(_manifest(), V0, _no_tags(_manifest()), pinned_commits={"dash": 1},
                                cargo_versions={"dash": "0.42.0"})[0]
    calls, fake_run = _fake_git_run()
    rc = cli._release_execute([step], V0, str(tmp_path), {}, str(tmp_path / "coxswain"), fake_run,
                              {"components": {}}, str(tmp_path / "manifest.toml"))
    assert rc == 0
    assert capsys.readouterr().out.strip() == f"bump_cargo dash: 0.42.0 -> {V0}"
    assert f'version = "{V0}"' in (dash / "Cargo.toml").read_text()
    assert (dash / "Cargo.lock").read_text().count(f'"{V0}"') == 1
    assert release.commit_argv(str(dash), f"cargo: bump to {V0} to match the tag", "Cargo.toml", "Cargo.lock") in calls


def test_release_execute_a_bump_already_at_the_version_no_ops_its_land_steps(tmp_path):
    umbrella = _checkout(tmp_path, _manifest_toml_at(V1))
    steps = [s for s in release.release_plan(_manifest(), V1, _no_tags(_manifest()))
             if s["kind"] in ("bump_version", "push", "pr_create", "wait_checks", "merge")]
    calls, fake_run = _fake_git_run()
    rc = cli._release_execute(steps, V1, str(tmp_path), {}, str(umbrella), fake_run, {"components": {}},
                              str(umbrella / "manifest.toml"))
    assert rc == 0 and calls == []


def test_notes_step_rewrites_the_index_and_the_bump_commit_carries_it(tmp_path):
    umbrella = _checkout(tmp_path, notes="notes", version=V1)
    (umbrella / "docs" / "releases" / "index.md").write_text(f"## `{V0}`\n\nold\n")
    steps = [s for s in release.release_plan(_manifest(), V1, _no_tags(_manifest())) if s["kind"] in ("notes", "bump_version")]
    calls, fake_run = _fake_git_run()
    rc = cli._release_execute(steps, V1, str(tmp_path), {}, str(umbrella), fake_run, _manifest(),
                              str(umbrella / "manifest.toml"))
    assert rc == 0
    assert f"## `{V1}`" in (umbrella / "docs" / "releases" / "index.md").read_text()
    assert release.add_argv(str(umbrella), "manifest.toml", "docs/releases/index.md") in calls


def test_merge_refuses_naming_the_dirty_file_before_merging_or_pulling(tmp_path, capsys):
    calls = []

    def run(argv, cwd):
        calls.append(argv)
        return (0, " M docs/releases/index.md\n") if argv[-2:] == ["status", "--porcelain"] else (0, "")
    rc = cli._release_execute([{"kind": "merge", "component": "coxswain"}], V1, str(tmp_path), {}, str(tmp_path), run, {}, "")
    assert rc == 2
    assert capsys.readouterr().out.strip().splitlines() == [
        f"FAILED merge coxswain: {tmp_path} has uncommitted changes: docs/releases/index.md"]
    assert not any(c[0] == "gh" or "pull" in c for c in calls)


def test_cli_release_edits_an_existing_github_release_instead_of_creating_one(tmp_path, monkeypatch, no_tap):
    calls, inner = _fake_git_run()

    def run(argv, cwd):
        if argv[:3] == ["gh", "release", "view"]:
            calls.append(argv)
            return (0, "")
        return inner(argv, cwd)
    rc, _, _ = _cut(tmp_path, monkeypatch, fake=(calls, run))
    assert rc == 0
    assert [(c[2], c[3]) for c in calls if c[:2] == ["gh", "release"]] == [("view", f"v{V0}"), ("edit", f"v{V0}")]


def test_cli_release_writes_dashs_section_and_link_line_to_its_github_release(tmp_path, monkeypatch, no_tap):
    rc, calls, _ = _cut(tmp_path, monkeypatch, fake=_fake_git_run(commits=1),
                        notes="## coxswain-dash\ndash notes\n\n## Known gaps\nnone\n")
    assert rc == 0
    dash_create = next(c for c in calls if c[:3] == ["gh", "release", "create"] and c[6] == f"coxswain-dash {V0}")
    content = Path(dash_create[-1]).read_text()
    assert content.startswith("## coxswain-dash\ndash notes\n")
    assert f"https://github.com/ppfenning/coxswain/releases/tag/v{V0}" in content


def test_cli_release_names_first_release_for_a_dash_with_no_section_and_no_earlier_notes(tmp_path, monkeypatch, no_tap):
    rc, calls, _ = _cut(tmp_path, monkeypatch, fake=_fake_git_run(commits=1))
    assert rc == 0
    dash_create = next(c for c in calls if c[:3] == ["gh", "release", "create"] and c[6] == f"coxswain-dash {V0}")
    assert Path(dash_create[-1]).read_text().startswith("first release")


def test_cli_release_names_the_previous_releases_dash_tag_when_its_section_is_absent(tmp_path, monkeypatch, no_tap):
    umbrella = _checkout(tmp_path)
    (umbrella / "docs" / "releases" / "0.47.0.md").write_text("old notes\n")
    calls, inner = _fake_git_run(commits=1)

    def run(argv, cwd):
        if argv[0] == "git" and argv[3] == "show":
            return (0, _MANIFEST_TOML.replace("v0.42.0", "v0.41.0"))
        return inner(argv, cwd)
    rc, _, _ = _cut(tmp_path, monkeypatch, fake=(calls, run))
    assert rc == 0
    dash_create = next(c for c in calls if c[:3] == ["gh", "release", "create"] and c[6] == f"coxswain-dash {V0}")
    assert Path(dash_create[-1]).read_text().startswith("unchanged since v0.41.0")


def test_cli_release_refuses_before_any_tag_when_the_checkout_is_dirty_or_off_its_default_branch(tmp_path, monkeypatch, capsys):
    umbrella = str(tmp_path / "coxswain")
    for fake, said in ((_fake_git_run(dirty={umbrella}), "is dirty"),
                       (_fake_git_run(off_branch={umbrella}), "is on feature/x, not main")):
        rc, calls, _ = _cut(tmp_path, monkeypatch, fake=fake)
        assert rc == 2 and said in capsys.readouterr().out
        assert not any(c[3] in ("tag", "push") for c in calls)


def test_cli_release_refuses_before_any_tag_when_the_release_note_is_missing(tmp_path, monkeypatch):
    rc, calls, _ = _cut(tmp_path, monkeypatch, notes=None)
    assert rc == 2
    assert not any(c[3] in ("tag", "push") for c in calls)


def test_cli_release_missing_manifest_fails_gracefully_not_a_traceback(tmp_path, capsys):
    rc = cli.main(["release", V0, "--dry-run", "--manifest", str(tmp_path / "manifest.toml")])
    assert rc == 2 and "refusing" in capsys.readouterr().out


def test_cli_release_exits_two_and_names_the_repository_on_an_existing_tag(tmp_path, capsys, monkeypatch):
    umbrella = _checkout(tmp_path)
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: [f"v{V0}"] if repo == "ppfenning/coxswain" else [])
    monkeypatch.setattr(cli, "_real_run", _fake_git_run()[1])
    rc = cli.main(["release", V0, "--dry-run", "--manifest", str(umbrella / "manifest.toml")])
    out = capsys.readouterr().out
    assert rc == 2 and f"tag v{V0} already exists on coxswain" in out


def test_cli_release_refuses_off_a_non_maintainer_checkout(tmp_path, capsys, monkeypatch):
    umbrella = _checkout(tmp_path)
    monkeypatch.setattr(cli, "_maintainer_remote_url", lambda directory: "git@github.com:someone/else.git")
    rc = cli.main(["release", V0, "--dry-run", "--manifest", str(umbrella / "manifest.toml")])
    out = capsys.readouterr().out
    assert rc == 2
    assert len(out.strip().splitlines()) == 1 and "ppfenning/coxswain" in out


def test_cli_release_reads_dash_from_a_checkout_override(tmp_path, monkeypatch, capsys):
    dash = tmp_path / "elsewhere" / "towpath"
    dash.mkdir(parents=True)
    (dash / "Cargo.toml").write_text('[package]\nname = "towpath"\nversion = "0.42.0"\n')
    rc, _, _ = _cut(tmp_path, monkeypatch, V0, "--dry-run", "--checkout", f"dash={dash}", fake=_fake_git_run(commits=1))
    out = capsys.readouterr().out
    assert rc == 0
    assert f"bump_cargo dash: Cargo.toml 0.42.0 -> {V0} on release/{V0}" in out


def test_cli_release_refuses_on_a_real_versions_drift_even_with_allow_doc_drift(tmp_path, capsys, monkeypatch):
    umbrella = _checkout(tmp_path, notes="notes")
    (umbrella / "pyproject.toml").write_text('[project]\nname = "coxswain"\nversion = "2026.9.0"\n')
    rc, _, _ = _cut(tmp_path, monkeypatch, V0, "--dry-run", "--allow-doc-drift", "shipping anyway")
    out = capsys.readouterr().out
    assert rc == 2
    assert "refuse versions" in out and "umbrella pyproject.toml is 2026.9.0" in out


def test_cli_release_refuses_before_tagging_when_a_drift_stands_and_notes_it_when_allowed(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(release_check, "CHECKS", (lambda facts: [Drift("cli-surface", "docs/x.md", None, "cli.py", 10, "add x")],))
    rc, _, _ = _cut(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 2
    assert "refuse cli-surface: cli-surface: docs/x.md <-> cli.py:10 — add x" in out and "tag_self" not in out
    rc, _, _ = _cut(tmp_path, monkeypatch, V0, "--dry-run", "--allow-doc-drift", "docs land next sprint")
    out = capsys.readouterr().out
    assert rc == 0
    assert "note release-check: docs land next sprint (1 drift allowed)" in out


# ---- the rehearsal gate's edge ----


_REHEARSAL_NOTES_LINE = "Cut with `--allow-no-rehearsal`: the runner image was unavailable."


def _sha_of(directory):
    return hashlib.sha1(str(directory).encode()).hexdigest()


def _rehearsal_edge(runs, jobs=()):
    """A runner for the gate's edge: origin commits are a hash of the checkout path, the checkout's one tag is
    v0.47.0, and the rehearsal workflow lists `runs`. Anything else is the clean fake git."""
    git = _fake_git_run()[1]

    def run(argv, cwd):
        if argv[:3] == ["gh", "run", "list"] and "release-rehearsal.yml" in argv:
            return 0, json.dumps(runs)
        if argv[:3] == ["gh", "run", "view"] and "jobs" in argv:
            return 0, json.dumps({"jobs": list(jobs)})
        if argv[3:4] == ["rev-parse"] and argv[-1].startswith("origin/"):
            return 0, _sha_of(argv[2]) + "\n"
        if argv[3:5] == ["tag", "--list"]:
            return 0, "v0.47.0\nv0.46.0\n"
        return git(argv, cwd)
    return run


@pytest.fixture
def rehearsal_runner(monkeypatch):
    def use(runs, jobs=()):
        monkeypatch.setattr(cli, "_stdout_run", _rehearsal_edge(runs, jobs))
    return use


def test_cli_release_refuses_before_any_tag_step_when_no_rehearsal_run_matches(tmp_path, capsys, monkeypatch, rehearsal_runner):
    monkeypatch.setattr(cli, "_rehearsal_steps", _REAL_REHEARSAL_STEPS)
    umbrella = _checkout(tmp_path, notes="")
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: [])
    monkeypatch.setattr(cli, "_real_run", _fake_git_run()[1])
    rehearsal_runner([])
    rc = cli.main(["release", V0, "--manifest", str(umbrella / "manifest.toml")])
    out = capsys.readouterr().out
    title = release_rehearsal_gate.run_title(_sha_of(umbrella), "pinned", "v0.47.0", [])
    assert rc == 2
    assert f"refuse rehearsal: No rehearsal run matches. Looked for: {title}" in out
    assert "tag_self" not in out


def test_cli_release_proceeds_when_the_matching_rehearsal_run_passed(tmp_path, capsys, monkeypatch, rehearsal_runner):
    monkeypatch.setattr(cli, "_rehearsal_steps", _REAL_REHEARSAL_STEPS)
    umbrella = _checkout(tmp_path, notes="")
    title = release_rehearsal_gate.run_title(_sha_of(umbrella), "pinned", "v0.47.0", [])
    runs = [{"databaseId": 7, "displayTitle": title, "conclusion": "success", "createdAt": "2026-10-01T10:00:00Z"}]
    jobs = [{"name": "fresh", "conclusion": "success"}, {"name": "upgrade", "conclusion": "success"},
            {"name": "migrate", "conclusion": "skipped"}]
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: [])
    monkeypatch.setattr(cli, "_real_run", _fake_git_run()[1])
    rehearsal_runner(runs, jobs)
    rc = cli.main(["release", V0, "--dry-run", "--manifest", str(umbrella / "manifest.toml")])
    assert rc == 0
    assert "note rehearsal: rehearsal passed: gh run view 7" in capsys.readouterr().out


def test_cli_release_with_allow_no_rehearsal_and_the_matching_notes_line_proceeds(tmp_path, capsys, monkeypatch, rehearsal_runner):
    monkeypatch.setattr(cli, "_rehearsal_steps", _REAL_REHEARSAL_STEPS)
    umbrella = _checkout(tmp_path, notes=f"## Known gaps\n\n{_REHEARSAL_NOTES_LINE}\n")
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: [])
    monkeypatch.setattr(cli, "_real_run", _fake_git_run()[1])
    rehearsal_runner([])
    rc = cli.main(["release", V0, "--dry-run", "--allow-no-rehearsal", "the runner image was unavailable",
                   "--manifest", str(umbrella / "manifest.toml")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "note rehearsal: the runner image was unavailable" in out
    assert f"tag_self coxswain: v{V0}" in out


def test_rehearsal_steps_name_dashs_commit_when_the_release_tags_it(tmp_path, rehearsal_runner, monkeypatch):
    umbrella = _checkout(tmp_path, notes="")
    plan = release.release_plan(_manifest(), V0, _no_tags(_manifest()), pinned_commits={"dash": 1})
    seen = {}
    monkeypatch.setattr(release_rehearsal_gate, "decide", lambda runs, jobs_of, shas, *rest: seen.update(shas) or [])

    class Args:
        version, allow_no_rehearsal = V0, None
    _REAL_REHEARSAL_STEPS(Args, plan, str(tmp_path), {}, str(umbrella), _rehearsal_edge([]))
    assert seen == {"coxswain": _sha_of(umbrella), "dash": _sha_of(tmp_path / "dash")}


# ---- backfill ----


_BACKFILL_MANIFEST = """
[coxswain]
version = "0.47.0"
repo = "org/coxswain"

[components.harness]
repo = "org/harness"
tag = "v0.47.0"
"""

_BACKFILL_CALVER_MANIFEST = f"""
[coxswain]
version = "{V0}"
repo = "org/coxswain"

[components]
dash = {{ repo = "org/dash", tag = "v0.42.0", flag = "dash" }}
"""

_BACKFILL_HARNESS_BODY = ("## coxswain-harness\nharness notes\n"
                          "\nSee the full release notes: https://github.com/org/coxswain/releases/tag/v0.47.0\n")


def _backfill_umbrella(tmp_path, notes=None):
    umbrella = tmp_path / "coxswain"
    (umbrella / "docs" / "releases").mkdir(parents=True, exist_ok=True)
    for version, text in (notes or {"0.47.0": "## coxswain-harness\nharness notes\n"}).items():
        (umbrella / "docs" / "releases" / f"{version}.md").write_text(text)
    for name in ("harness", "crew", "dash"):
        (tmp_path / name).mkdir(exist_ok=True)  # a component with no checkout is skipped, so give them one
    return umbrella


def _fake_backfill_run(manifest_by_version, view_bodies=None):
    calls: list = []
    view_bodies = view_bodies or {}

    def run(argv, cwd):
        calls.append(argv)
        if argv[0] == "git" and argv[3] == "show":
            return (0, manifest_by_version[argv[4].split(":")[0][1:]])
        if argv[:3] == ["gh", "release", "view"]:
            key = (cwd, argv[3])
            return (0, view_bodies[key]) if key in view_bodies else (1, "release not found")
        return (0, "")
    return calls, run


def _backfill(monkeypatch, umbrella, run, *flags):
    monkeypatch.setattr(cli, "_real_run", run)
    return cli.main(["backfill-github-releases", "--umbrella", str(umbrella), *flags])


def test_backfill_creates_a_release_for_the_checkout_and_each_component_tagged_at_that_version(tmp_path, monkeypatch, capsys):
    umbrella = _backfill_umbrella(tmp_path)
    calls, run = _fake_backfill_run({"0.47.0": _BACKFILL_MANIFEST})
    assert _backfill(monkeypatch, umbrella, run) == 0
    assert {"created org/coxswain v0.47.0", "created org/harness v0.47.0"} <= set(capsys.readouterr().out.splitlines())
    harness_create = next(c for c in calls if c[:3] == ["gh", "release", "create"] and c[6] == "coxswain-harness 0.47.0")
    assert Path(harness_create[-1]).read_text().endswith(
        "See the full release notes: https://github.com/org/coxswain/releases/tag/v0.47.0\n")


def test_backfill_skips_a_component_with_no_checkout_instead_of_crashing(tmp_path, monkeypatch, capsys):
    umbrella = _backfill_umbrella(tmp_path)
    manifest = _BACKFILL_MANIFEST + '\n[components.hud]\nrepo = "org/hud"\ntag = "v0.47.0"\n'
    _, run = _fake_backfill_run({"0.47.0": manifest})
    assert _backfill(monkeypatch, umbrella, run, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "skipped org/hud v0.47.0: no checkout at" in out
    assert "would create org/harness v0.47.0" in out


def test_backfill_reads_bodies_through_json_and_says_already_current_or_edited(tmp_path, monkeypatch, capsys):
    umbrella = _backfill_umbrella(tmp_path)
    calls, run = _fake_backfill_run({"0.47.0": _BACKFILL_MANIFEST}, view_bodies={
        (str(tmp_path / "harness"), "v0.47.0"): _BACKFILL_HARNESS_BODY, (str(umbrella), "v0.47.0"): "a stale body\n"})
    assert _backfill(monkeypatch, umbrella, run) == 0
    out = capsys.readouterr().out
    assert "already-current org/harness v0.47.0" in out and "edited org/coxswain v0.47.0" in out
    assert all(c[-4:] == ["--json", "body", "-q", ".body"] for c in calls if c[:3] == ["gh", "release", "view"])


def test_backfill_a_failed_create_call_prints_failed_and_stops(tmp_path, monkeypatch, capsys):
    umbrella = _backfill_umbrella(tmp_path)
    _, base = _fake_backfill_run({"0.47.0": _BACKFILL_MANIFEST})
    rc = _backfill(monkeypatch, umbrella, lambda argv, cwd: (1, "boom") if argv[:3] == ["gh", "release", "create"] else base(argv, cwd))
    out = capsys.readouterr().out
    assert rc == 2 and "FAILED org/coxswain v0.47.0: boom" in out and "org/harness" not in out


def test_backfill_dry_run_makes_no_create_or_edit_calls(tmp_path, monkeypatch, capsys):
    umbrella = _backfill_umbrella(tmp_path)
    calls, run = _fake_backfill_run({"0.47.0": _BACKFILL_MANIFEST})
    assert _backfill(monkeypatch, umbrella, run, "--dry-run") == 0
    assert "would create org/coxswain v0.47.0" in capsys.readouterr().out
    assert [c for c in calls if c[:3] in (["gh", "release", "create"], ["gh", "release", "edit"])] == []


def test_backfill_walks_the_semver_releases_before_the_calver_ones(tmp_path, monkeypatch, capsys):
    umbrella = _backfill_umbrella(tmp_path, {V0: "## coxswain-dash\ndash\n", "0.47.0": "## coxswain-harness\nh\n"})
    _, run = _fake_backfill_run({"0.47.0": _BACKFILL_MANIFEST, V0: _BACKFILL_CALVER_MANIFEST})
    assert _backfill(monkeypatch, umbrella, run) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("created")]
    assert lines.index("created org/coxswain v0.47.0") < lines.index(f"created org/coxswain v{V0}")
    # dash keeps v0.42.0 at the CalVer release, so it has no release of its own there.
    assert f"created org/dash v{V0}" not in lines


def test_backfill_a_pinned_component_is_only_processed_at_the_version_it_was_tagged(tmp_path, monkeypatch):
    umbrella = _backfill_umbrella(tmp_path, {"0.4.0": "## coxswain-crew\ncrew notes\n", "0.6.0": "nothing\n"})
    pinned = '[coxswain]\nversion = "{v}"\nrepo = "org/coxswain"\n\n[components.crew]\nrepo = "org/crew"\ntag = "v0.4.0"\nlockstep = false\n'
    calls, run = _fake_backfill_run({"0.4.0": pinned.format(v="0.4.0"), "0.6.0": pinned.format(v="0.6.0")})
    assert _backfill(monkeypatch, umbrella, run) == 0
    crew = [c[6] for c in calls if c[:3] == ["gh", "release", "create"] and c[6].startswith("coxswain-crew")]
    assert crew == ["coxswain-crew 0.4.0"]
