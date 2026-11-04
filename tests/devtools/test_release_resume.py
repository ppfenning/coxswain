from devtools import cli, release

_V = "2026.11.0"
_BRANCH = f"release/{_V}"
_BUMP = f"{_BRANCH}-bump"
_TAG = f"v{_V}"
_SHA = "a" * 40
_OTHER = "b" * 40
_MANIFEST = {"coxswain": {"version": _V}, "components": {}}
_PYPROJECT_AT_1_1 = '[project]\nname = "dash"\nversion = "1.1.0"\n'


class Origin:
    """A recording runner that models origin: refs, files at refs, ancestry and published releases."""

    def __init__(self, refs=None, files=None, ancestors=(), releases=None, merge_back_fails=False):
        self.refs = dict(refs or {})
        self.files = dict(files or {})
        self.ancestors = set(ancestors)
        self.releases = dict(releases or {})
        self.merge_back_fails = merge_back_fails
        self.calls: list = []

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        if argv[:1] == ["git"]:
            return self._git(argv[3], argv[4:])
        if argv[:3] == ["gh", "release", "view"]:
            body = self.releases.get(argv[3])
            return (1, "release not found") if body is None else (0, body if "--json" in argv else "title\n")
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, self.refs.get("open-prs", "[]")
        if argv[:3] == ["gh", "pr", "checks"]:
            return 0, "build\tpass\t1s\thttps://example.test/1\n"
        if argv[:3] == ["gh", "pr", "merge"] and self.merge_back_fails:
            return 1, "merge blocked"
        return 0, ""

    def _git(self, sub, rest):
        if sub == "rev-parse":
            return 0, "main\n"
        if sub == "rev-list":
            return 0, "0\t0\n"
        if sub == "symbolic-ref":
            return 1, ""
        if sub == "ls-remote":
            ref = rest[1]
            lines = [f"{self.refs[name]}\t{name}\n" for name in (ref, f"{ref}^{{}}") if name in self.refs]
            return 0, "".join(lines)
        if sub == "merge-base":
            return (0, "") if (rest[1], rest[2]) in self.ancestors else (1, "")
        if sub == "show":
            text = self.files.get(rest[0])
            return (1, "fatal: path missing") if text is None else (0, text)
        return 0, ""

    def verbs(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def _world(tmp_path):
    umbrella = tmp_path / "coxswain"
    (umbrella / "docs" / "releases").mkdir(parents=True)
    (umbrella / "manifest.toml").write_text('[coxswain]\nversion = "2026.10.0"\n')
    (umbrella / "docs" / "releases" / f"{_V}.md").write_text("notes\n")
    (tmp_path / "dash").mkdir()
    (tmp_path / "dash" / "pyproject.toml").write_text('[project]\nname = "dash"\nversion = "1.0.0"\n')
    return str(tmp_path), str(umbrella), str(tmp_path / "dash")


def _execute(tmp_path, steps, origin, rehearsed=None):
    root, umbrella, dash = _world(tmp_path)
    rc = cli._release_execute(steps, _V, root, {}, umbrella, origin, _MANIFEST, f"{umbrella}/manifest.toml",
                              sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__, rehearsed=rehearsed)
    return rc, dash, umbrella


def _tag_step(**extra):
    return {"kind": "tag", "component": "dash", "tag": _TAG, "ref": _BRANCH, **extra}


def test_a_rerun_with_the_tag_present_skips_to_the_next_step(tmp_path, capsys):
    origin = Origin(refs={f"refs/tags/{_TAG}": "c" * 40, f"refs/tags/{_TAG}^{{}}": _SHA, f"refs/heads/{_BRANCH}": _SHA})
    steps = [_tag_step(), {"kind": "pinned", "component": "crew", "tag": "v1"}]
    rc, dash, _ = _execute(tmp_path, steps, origin)
    out = capsys.readouterr().out
    assert rc == 0
    assert origin.verbs("git", "-C", dash, "tag") == []
    assert origin.verbs("git", "-C", dash, "push") == []
    assert f"tag dash: {_TAG} already at {_SHA}" in out
    assert "pinned crew: v1" in out


def test_a_tag_at_another_commit_fails_naming_both_shas(tmp_path, capsys):
    origin = Origin(refs={f"refs/tags/{_TAG}": _OTHER, f"refs/heads/{_BRANCH}": _SHA})
    rc, dash, _ = _execute(tmp_path, [_tag_step()], origin)
    assert rc == 2
    assert capsys.readouterr().out.strip() == (
        f"FAILED tag dash: {_TAG} is at {_OTHER} but origin/{_BRANCH} is at {_SHA}")
    assert origin.verbs("git", "-C", dash, "push") == []


def test_a_missing_tag_is_still_made(tmp_path):
    origin = Origin(refs={f"refs/heads/{_BRANCH}": _SHA})
    rc, dash, _ = _execute(tmp_path, [_tag_step()], origin)
    assert rc == 0
    assert release.tag_argv(dash, _V, f"origin/{_BRANCH}") in origin.calls


def test_tag_self_with_the_tag_present_is_skipped(tmp_path, capsys):
    origin = Origin(refs={f"refs/tags/{_TAG}": _SHA, f"refs/heads/{_BRANCH}": _SHA})
    step = {"kind": "tag_self", "component": "coxswain", "tag": _TAG, "ref": _BRANCH}
    rc, _, umbrella = _execute(tmp_path, [step], origin)
    assert rc == 0
    assert origin.verbs("git", "-C", umbrella, "tag") == []
    assert f"tag_self coxswain: {_TAG} already at {_SHA}" in capsys.readouterr().out


def _release_step(tmp_path):
    notes = tmp_path / "coxswain" / "docs" / "releases" / f"{_V}.md"
    return {"kind": "github_release", "component": "coxswain", "tag": _TAG, "title": _V, "heading": None,
            "notes_path": f"docs/releases/{_V}.md"}, notes


def test_an_existing_release_with_the_same_body_is_skipped(tmp_path, capsys):
    step, _ = _release_step(tmp_path)
    origin = Origin(releases={_TAG: "notes\r\n"})
    rc, _, umbrella = _execute(tmp_path, [step], origin)
    assert rc == 0
    assert origin.verbs("gh", "release", "edit") == []
    assert origin.verbs("gh", "release", "create") == []
    assert f"github_release coxswain: {_TAG} already published" in capsys.readouterr().out


def test_an_existing_release_with_another_body_is_edited(tmp_path):
    step, _ = _release_step(tmp_path)
    origin = Origin(releases={_TAG: "older notes\n"})
    rc, _, umbrella = _execute(tmp_path, [step], origin)
    assert rc == 0
    assert len(origin.verbs("gh", "release", "edit")) == 1
    assert origin.verbs("gh", "release", "create") == []


def test_a_bump_already_on_the_release_branch_skips_push_pull_request_and_merge(tmp_path, capsys):
    origin = Origin(files={f"origin/{_BRANCH}:pyproject.toml": _PYPROJECT_AT_1_1})
    steps = [
        {"kind": "bump_pyproject", "component": "dash", "from": "1.0.0", "to": "1.1.0", "branch": _BUMP,
         "commit_subject": "Bump dash", "base": _BRANCH},
        {"kind": "push", "component": "dash", "branch": _BUMP, "base": _BRANCH},
        {"kind": "pr_create", "component": "dash", "title": "Bump dash", "body": "body", "base": _BRANCH},
        {"kind": "wait_checks", "component": "dash", "base": _BRANCH},
        {"kind": "merge", "component": "dash", "base": _BRANCH},
    ]
    rc, dash, _ = _execute(tmp_path, steps, origin)
    assert rc == 0
    assert origin.calls == [release.show_file_argv(dash, f"origin/{_BRANCH}", "pyproject.toml")]
    assert capsys.readouterr().out.count("already at 1.1.0") == 5


def test_a_bump_not_yet_on_the_release_branch_is_made(tmp_path):
    origin = Origin(files={f"origin/{_BRANCH}:pyproject.toml": '[project]\nname = "dash"\nversion = "1.0.0"\n'})
    step = {"kind": "bump_pyproject", "component": "dash", "from": "1.0.0", "to": "1.1.0", "branch": _BUMP,
            "commit_subject": "Bump dash", "base": _BRANCH}
    rc, dash, _ = _execute(tmp_path, [step], origin)
    assert rc == 0
    assert release.checkout_branch_argv(dash, _BUMP, f"origin/{_BRANCH}") in origin.calls


def test_a_release_branch_holding_the_rehearsed_sha_is_done(tmp_path, capsys):
    origin = Origin(refs={f"refs/heads/{_BRANCH}": _OTHER}, ancestors={(_SHA, _OTHER)})
    step = {"kind": "release_branch", "component": "dash", "branch": _BRANCH}
    rc, dash, _ = _execute(tmp_path, [step], origin, rehearsed={"dash": _SHA})
    assert rc == 0
    assert release.release_branch_push_argv(dash, _SHA, _BRANCH) not in origin.calls
    assert f"release_branch dash: {_BRANCH} already at {_OTHER}, which holds {_SHA}" in capsys.readouterr().out


def test_a_release_branch_without_the_rehearsed_sha_fails_naming_both(tmp_path, capsys):
    origin = Origin(refs={f"refs/heads/{_BRANCH}": _OTHER})
    step = {"kind": "release_branch", "component": "dash", "branch": _BRANCH}
    rc, dash, _ = _execute(tmp_path, [step], origin, rehearsed={"dash": _SHA})
    assert rc == 2
    assert capsys.readouterr().out.strip() == (
        f"FAILED release_branch dash: {_BRANCH} is at {_OTHER}, which does not hold the rehearsed {_SHA}")
    assert release.release_branch_push_argv(dash, _SHA, _BRANCH) not in origin.calls


def test_merge_back_runs_the_sequence_into_the_default_branch(tmp_path, capsys):
    origin = Origin(refs={f"refs/heads/{_BRANCH}": _SHA})
    step = {"kind": "merge_back", "component": "dash", "branch": _BRANCH}
    rc, dash, _ = _execute(tmp_path, [step], origin)
    assert rc == 0
    ordered = [
        release.checkout_tracking_argv(dash, _BRANCH),
        ["gh", "pr", "list", "--head", _BRANCH, "--base", "main", "--state", "open", "--json", "number"],
        release.pr_create_argv(f"Merge {_BRANCH} back into main",
                               f"Merges {_BRANCH} back into main so the release tag stays an ancestor of main.",
                               "main", _BRANCH),
        release.pr_checks_argv(_BRANCH),
        release.pr_merge_argv(_BRANCH, "merge"),
        release.checkout_ref_argv(dash, "main"),
        release.pull_argv(dash, "main"),
    ]
    positions = [origin.calls.index(argv) for argv in ordered]
    assert positions == sorted(positions)
    assert ["gh", "pr", "create", "--title", f"Merge {_BRANCH} back into main", "--body",
            f"Merges {_BRANCH} back into main so the release tag stays an ancestor of main.",
            "--base", "main", "--head", _BRANCH] in origin.calls
    assert origin.verbs("gh", "pr", "merge")[0][-2:] == ["--merge", "--delete-branch"]
    assert f"merge_back dash: {_BRANCH} merged into main" in capsys.readouterr().out


def test_merge_back_reuses_an_open_pull_request(tmp_path):
    origin = Origin(refs={f"refs/heads/{_BRANCH}": _SHA, "open-prs": '[{"number": 7}]'})
    rc, _, _ = _execute(tmp_path, [{"kind": "merge_back", "component": "dash", "branch": _BRANCH}], origin)
    assert rc == 0
    assert origin.verbs("gh", "pr", "create") == []
    assert len(origin.verbs("gh", "pr", "merge")) == 1


def test_merge_back_runs_after_the_tag_steps(tmp_path):
    origin = Origin(refs={f"refs/heads/{_BRANCH}": _SHA})
    steps = [_tag_step(), {"kind": "merge_back", "component": "dash", "branch": _BRANCH}]
    rc, dash, _ = _execute(tmp_path, steps, origin)
    assert rc == 0
    assert origin.calls.index(release.push_argv(dash, _V)) < origin.calls.index(release.checkout_tracking_argv(dash, _BRANCH))


def test_merge_back_with_the_branch_gone_is_reported_done(tmp_path, capsys):
    origin = Origin()
    rc, dash, _ = _execute(tmp_path, [{"kind": "merge_back", "component": "dash", "branch": _BRANCH}], origin)
    assert rc == 0
    assert origin.verbs("git", "-C", dash, "checkout") == []
    assert origin.verbs("gh") == []
    assert f"merge_back dash: {_BRANCH} already merged back and deleted" in capsys.readouterr().out


def test_a_merge_back_failure_returns_2(tmp_path, capsys):
    origin = Origin(refs={f"refs/heads/{_BRANCH}": _SHA}, merge_back_fails=True)
    rc, dash, _ = _execute(tmp_path, [{"kind": "merge_back", "component": "dash", "branch": _BRANCH}], origin)
    assert rc == 2
    assert capsys.readouterr().out.strip() == "FAILED merge_back dash: merge blocked"
    assert release.pull_argv(dash, "main") not in origin.calls
