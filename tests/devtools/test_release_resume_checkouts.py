"""A resume reads the release's own files, so it plans the same from the release's checkout and from a main that moved on.

The fake answers `git show <ref>:<path>` for origin's release branch, the recorded commit and the tag from `RELEASE_FILES`;
the checkout on disk holds the same files, main's newer ones, or main's newer ones as an unfinished draft.
The dry run prints each plan step."""

import pytest
from test_release import V0, V1, _checkout, _manifest_toml_at
from test_release_cut_record import _REAL_REHEARSAL_STEPS, Edge, _record, _runs
from test_release_notes_gate import _BOTH

from devtools import cli, release

_PREVIOUS = "2026.9.0"
_BRANCH = f"refs/heads/release/{V0}"
_TAG = f"refs/tags/v{V0}"
_RECORDED = "ee" * 20
_PYPROJECT = f'[project]\nname = "coxswain"\nversion = "{_PREVIOUS}"\n'
RELEASE_FILES = {
    "manifest.toml": _manifest_toml_at(_PREVIOUS),
    f"docs/releases/{V0}.md": "notes\n",
    "pyproject.toml": _PYPROJECT,
    "docs/releases/index.md": "",
}
_REMAINING = (
    "release_branch coxswain",
    "tag_self coxswain",
    "github_release coxswain",
    "merge_back coxswain",
    "tap_formula_pr tap",
)
_STATES = ("rehearsal", "tag", "merge-back")


class Resume(Edge):
    """`Edge` plus origin's release branch and tag, the release's files at `source`, and a commit count of 3 for dash,
    so it is tagged with the release and has a merge-back of its own. With `merged`, dash's branch is gone."""

    def __init__(self, source, branch, tag=False, merged=False, files=None, **kw):
        super().__init__(**kw)
        self.source, self.branch, self.tag, self.merged = source, branch, tag, merged
        self.files = RELEASE_FILES if files is None else files

    def __call__(self, argv, cwd):
        if argv[3:4] == ["ls-remote"] and any(part == _BRANCH for part in argv):
            listed = self.branch and not (self.merged and argv[2].endswith("/dash"))
            return (0, f"{'cd' * 20}\t{_BRANCH}\n") if listed else (0, "")
        if argv[3:4] == ["ls-remote"] and any(part == _TAG for part in argv):
            return (0, f"{'cd' * 20}\t{_TAG}\n") if self.tag else (0, "")
        if argv[3:4] == ["show"] and argv[4].partition(":")[0] == self.source:
            self.calls.append(argv)
            path = argv[4].partition(":")[2]
            return (0, self.files[path]) if path in self.files else (1, "fatal: path not in the tree")
        if argv[3:4] == ["rev-list"] and "--count" in argv and "--left-right" not in argv:
            return 0, "3\n"
        return super().__call__(argv, cwd)

    def shown(self):
        return [argv for argv in self.calls if argv[3:4] == ["show"]]


@pytest.fixture
def resume(tmp_path, monkeypatch, capsys):
    """`resume(state, disk)`: a run of V0 for a release stopped at `state`, from a checkout holding the release's
    files (`disk="release"`), a main that moved on to V1 (`"main"`), or that main with draft notes (`"draft"`).
    `execute` runs without `--dry-run`, with the executor replaced by a recorder of what it is handed.
    Returns `(rc, out, edge, umbrella, handed)`."""
    monkeypatch.setattr(cli, "_maintainer_remote_url", lambda directory: "git@github.com:ppfenning/coxswain.git")
    monkeypatch.setattr(cli, "_tap_state", lambda directory: "clean")
    monkeypatch.setattr(cli, "_repository_url", lambda: "https://github.com/ppfenning/coxswain")
    monkeypatch.setattr(cli, "_component_declares_tag_trigger", lambda directory: True)
    monkeypatch.setattr(cli, "_rehearsal_steps", _REAL_REHEARSAL_STEPS)

    def run(state, disk, *, recorded=True, files=None, execute=False):
        branched = state in ("tag", "merge-back")
        tagged = state in ("tag", "merge-back", "tag-only")
        monkeypatch.setattr(cli, "_remote_tags", lambda repo: [f"v{V0}"] if tagged else [])
        if disk == "release":
            umbrella = _checkout(tmp_path, _manifest_toml_at(_PREVIOUS), notes="notes\n", version=V0)
            (umbrella / "pyproject.toml").write_text(_PYPROJECT)
        else:
            notes = _BOTH if disk == "draft" else "newer notes\n"
            umbrella = _checkout(tmp_path, _manifest_toml_at(V1), notes=notes, version=V1)
            (umbrella / "pyproject.toml").write_text(_PYPROJECT.replace(_PREVIOUS, V1))
        source = {"rehearsal": _RECORDED, "tag-only": f"v{V0}"}.get(state, f"origin/release/{V0}")
        edge = Resume(
            source=source,
            branch=branched,
            tag=tagged,
            merged=state == "merge-back",
            files=files,
            runs=_runs(umbrella),
            record=_record(umbrella, sha=_RECORDED) if recorded else None,
        )
        handed: dict = {}
        monkeypatch.setattr(cli, "_real_run", edge)
        monkeypatch.setattr(cli, "_stdout_run", edge)
        if execute:
            monkeypatch.setattr(cli, "_release_execute", lambda *args, **kw: handed.update(kw) or 0)
        rc = cli.main(
            ["release", V0, *([] if execute else ["--dry-run"]), "--manifest", str(umbrella / "manifest.toml")]
        )
        return rc, capsys.readouterr().out, edge, umbrella, handed

    return run


def _planned(out):
    return {" ".join(line.split(":")[0].split()[:2]) for line in out.splitlines()}


@pytest.mark.parametrize("disk", ["release", "main", "draft"])
@pytest.mark.parametrize("state", _STATES)
def test_a_resume_reaches_the_remaining_steps_from_either_checkout(resume, state, disk):
    rc, out, edge, *_ = resume(state, disk)
    assert "refuse" not in out and rc == 0, out
    assert set(_REMAINING) <= _planned(out) and "merge_back dash" in _planned(out)
    assert edge.pushes() == []


@pytest.mark.parametrize("state", ["rehearsal", "tag"])
def test_a_resume_from_main_reads_the_release_at_the_branch_or_the_recorded_commit(resume, state):
    _, _, edge, *_ = resume(state, "main")
    source = f"origin/release/{V0}" if state == "tag" else _RECORDED
    assert [f"{source}:manifest.toml", f"{source}:docs/releases/{V0}.md"] == [
        argv[4] for argv in edge.shown() if argv[4].split(":")[1] in ("manifest.toml", f"docs/releases/{V0}.md")
    ]


@pytest.mark.parametrize("state", ["rehearsal", "tag"])
def test_the_release_branch_is_cut_from_the_recorded_commit_not_from_main(resume, state):
    rc, _, _, _, handed = resume(state, "main", execute=True)
    assert rc == 0 and handed["rehearsed"]["coxswain"] == _RECORDED


def test_a_tag_alone_is_read_at_the_tag_and_refuses_on_the_collision_not_on_main_being_newer(resume):
    rc, out, edge, *_ = resume("tag-only", "main", recorded=False)
    assert rc == 2 and "tag v2026.10.0 already exists on coxswain" in out
    assert "not greater than" not in out
    assert {argv[4].split(":")[0] for argv in edge.shown()} == {f"v{V0}"}


def test_after_one_merge_back_the_merged_repo_skips_and_the_rest_remain(resume):
    rc, out, edge, umbrella, _ = resume("merge-back", "main")
    step = {"branch": f"release/{V0}"}
    ok, detail = cli._merge_back(step, str(umbrella.parent / "dash"), edge, None, None)
    assert rc == 0 and "merge_back coxswain" in _planned(out)
    assert (ok, detail) == (True, f"release/{V0} already merged back and deleted")


def test_a_release_with_no_state_still_refuses_from_a_main_that_moved_on(resume):
    rc, out, edge, *_ = resume("none", "main", recorded=False)
    assert rc == 2
    assert f"{V0} is not greater than the current version {V1}" in out
    assert edge.shown() == []


def test_a_release_with_no_state_and_a_draft_main_refuses_on_the_blanks_as_before(resume):
    rc, out, edge, *_ = resume("none", "draft", recorded=False)
    assert rc == 2 and "refuse notes_blank" in out
    assert edge.shown() == []


def test_the_releases_own_blank_notes_still_refuse(resume):
    rc, out, *_ = resume("tag", "release", files={**RELEASE_FILES, f"docs/releases/{V0}.md": _BOTH})
    assert rc == 2 and "notes_blank" in out


def test_a_release_whose_manifest_cannot_be_read_at_its_ref_refuses_instead_of_using_the_checkout(resume):
    files = {path: text for path, text in RELEASE_FILES.items() if path != "manifest.toml"}
    rc, out, *_ = resume("tag", "release", files=files)
    assert rc == 2 and f"refusing: no readable manifest.toml at origin/release/{V0}" in out


def test_the_release_source_prefers_the_branch_then_the_record_then_the_tag_then_nothing():
    record, sha = {"coxswain": "ab" * 20}, "cd" * 20
    assert cli._release_source(V0, record, sha, sha) == f"origin/{release.release_branch_name(V0)}"
    assert cli._release_source(V0, record, None, sha) == "ab" * 20
    assert cli._release_source(V0, None, None, sha) == f"v{V0}"
    assert cli._release_source(V0, None, None, None) is None


def test_a_component_with_the_tag_and_no_branch_is_resuming_only_while_the_umbrella_branch_is_listed(tmp_path):
    manifest = {"coxswain": {"version": V0}, "components": {"dash": {"repo": "org/dash", "tag": "v0.42.0"}}}
    tags = {"coxswain": [f"v{V0}"], "dash": [f"v{V0}"]}

    def origin(umbrella_listed):
        def run(argv, cwd):
            listed = umbrella_listed and argv[2].endswith("/coxswain")
            return (0, f"{'cd' * 20}\t{_BRANCH}\n") if listed else (0, "")

        return run

    args = (manifest, V0, {"dash": 3}, str(tmp_path / "coxswain"), str(tmp_path), {})
    assert cli._resuming(*args, origin(True), tags) == {"coxswain", "dash"}
    assert cli._resuming(*args, origin(False), tags) == set()
