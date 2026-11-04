from devtools import cli, release

_V = "2026.11.0"
_SHA = "a" * 40
_BRANCH = f"release/{_V}"
_BUMP = f"{_BRANCH}-bump"
_MANIFEST = {"coxswain": {"version": _V}, "components": {"dash": {"tag": "v2026.10.0"}}}


def _world(tmp_path):
    """An umbrella with a manifest and notes, and a dash checkout whose pyproject reads 1.0.0."""
    umbrella = tmp_path / "coxswain"
    (umbrella / "docs" / "releases").mkdir(parents=True, exist_ok=True)
    (umbrella / "manifest.toml").write_text('[coxswain]\nversion = "2026.10.0"\n')
    (umbrella / "docs" / "releases" / f"{_V}.md").write_text("notes\n")
    dash = tmp_path / "dash"
    dash.mkdir(exist_ok=True)
    (dash / "pyproject.toml").write_text('[project]\nname = "dash"\nversion = "1.0.0"\n')
    return str(tmp_path), str(umbrella), str(dash)


def _execute(tmp_path, steps, rehearsed=None):
    root, umbrella, _ = _world(tmp_path)
    calls: list = []

    def run(argv, cwd):
        calls.append(argv)
        if argv[:1] == ["git"] and argv[3:5] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if argv[:1] == ["git"] and argv[3] == "rev-list":
            return 0, "0\t0\n"
        if argv[:1] == ["git"] and argv[3] == "symbolic-ref":
            return 1, ""
        if argv[:3] == ["gh", "pr", "checks"]:
            return 0, "build\tpass\t1s\thttps://example.test/1\n"
        return 0, ""

    rc = cli._release_execute(steps, _V, root, {}, umbrella, run, _MANIFEST, str(tmp_path / "coxswain" / "manifest.toml"),
                              sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__, rehearsed=rehearsed)
    return rc, calls, umbrella, str(tmp_path / "dash")


def _land(base):
    """bump, push, pr, wait and merge for dash, with `base` when given."""
    extra = {"base": base} if base else {}
    return [
        {"kind": "bump_pyproject", "component": "dash", "from": "1.0.0", "to": "1.1.0",
         "branch": _BUMP if base else f"release/{_V}", "commit_subject": "Bump dash", **extra},
        {"kind": "push", "component": "dash", "branch": _BUMP if base else f"release/{_V}", **extra},
        {"kind": "pr_create", "component": "dash", "title": "Bump dash", "body": "body", **extra},
        {"kind": "wait_checks", "component": "dash", **extra},
        {"kind": "merge", "component": "dash", **extra},
    ]


def test_release_branch_pushes_the_rehearsed_sha_then_fetches(tmp_path, capsys):
    step = {"kind": "release_branch", "component": "dash", "branch": _BRANCH}
    rc, calls, _, dash = _execute(tmp_path, [step], rehearsed={"dash": _SHA})
    assert rc == 0
    assert calls == [release.release_branch_push_argv(dash, _SHA, _BRANCH), release.fetch_argv(dash)]
    assert capsys.readouterr().out.splitlines() == [f"release_branch dash: {_BRANCH} at {_SHA}"]


def test_release_branch_for_the_umbrella_reads_the_coxswain_sha(tmp_path):
    step = {"kind": "release_branch", "component": "coxswain", "branch": _BRANCH}
    rc, calls, umbrella, _ = _execute(tmp_path, [step], rehearsed={"coxswain": _SHA})
    assert rc == 0
    assert calls[0] == ["git", "-C", umbrella, "push", "origin", f"{_SHA}:refs/heads/{_BRANCH}"]


def test_release_branch_without_a_rehearsed_sha_fails(tmp_path, capsys):
    step = {"kind": "release_branch", "component": "dash", "branch": _BRANCH}
    for rehearsed in (None, {}, {"coxswain": _SHA}):
        rc, calls, _, _ = _execute(tmp_path, [step], rehearsed=rehearsed)
        assert rc == 2
        assert calls == []
    assert capsys.readouterr().out.count("FAILED release_branch dash") == 3


def test_bump_with_base_is_cut_from_origin_base(tmp_path):
    rc, calls, _, dash = _execute(tmp_path, _land(_BRANCH)[:1])
    assert rc == 0
    assert release.checkout_branch_argv(dash, _BUMP, f"origin/{_BRANCH}") in calls


def test_bump_pull_request_targets_the_release_branch(tmp_path):
    rc, calls, _, _ = _execute(tmp_path, _land(_BRANCH))
    assert rc == 0
    assert release.pr_create_argv("Bump dash", "body", _BRANCH) in calls
    assert ["gh", "pr", "create", "--title", "Bump dash", "--body", "body", "--base", _BRANCH] in calls


def test_merge_with_base_returns_to_the_release_branch_not_the_default(tmp_path):
    rc, calls, _, dash = _execute(tmp_path, _land(_BRANCH))
    assert rc == 0
    assert release.checkout_ref_argv(dash, _BRANCH) in calls
    assert release.pull_argv(dash, _BRANCH) in calls
    assert release.checkout_ref_argv(dash, "main") not in calls
    assert release.pull_argv(dash, "main") not in calls


def test_tag_with_ref_fetches_then_tags_origin_ref(tmp_path):
    steps = [{"kind": "tag", "component": "dash", "tag": "v" + _V, "ref": _BRANCH}]
    rc, calls, _, dash = _execute(tmp_path, steps)
    assert rc == 0
    tag = release.tag_argv(dash, _V, f"origin/{_BRANCH}")
    assert tag[-1] == f"origin/{_BRANCH}"
    assert calls[calls.index(tag) - 1] == release.fetch_argv(dash)
    assert calls[-1] == release.push_argv(dash, _V)


def test_tag_self_with_ref_tags_the_umbrella_on_origin_ref(tmp_path):
    steps = [{"kind": "tag_self", "component": "coxswain", "tag": "v" + _V, "ref": _BRANCH}]
    rc, calls, umbrella, _ = _execute(tmp_path, steps)
    assert rc == 0
    tag = release.tag_argv(umbrella, _V, f"origin/{_BRANCH}")
    assert tag in calls
    assert calls[calls.index(tag) - 1] == release.fetch_argv(umbrella)
    assert calls[-1] == release.push_argv(umbrella, _V)


def test_steps_without_the_new_fields_make_the_calls_they_always_made(tmp_path):
    steps = [*_land(None),
             {"kind": "tag", "component": "dash", "tag": "v" + _V},
             {"kind": "tag_self", "component": "coxswain", "tag": "v" + _V}]
    rc, calls, umbrella, dash = _execute(tmp_path, steps)
    assert rc == 0
    assert release.checkout_branch_argv(dash, _BRANCH, None) in calls
    assert ["gh", "pr", "create", "--title", "Bump dash", "--body", "body"] in calls
    assert release.checkout_ref_argv(dash, "main") in calls
    assert release.pull_argv(dash, "main") in calls
    assert release.tag_argv(dash, _V) in calls
    assert release.tag_argv(umbrella, _V) in calls
    flat = [word for call in calls for word in call]
    assert "--base" not in flat
    assert not [w for w in flat if w.startswith("origin/")]
    # only the readiness gate fetches: one per tagged checkout, none from the tag steps
    assert calls.count(release.fetch_argv(dash)) == 1
    assert calls.count(release.fetch_argv(umbrella)) == 1


def test_a_bump_that_is_already_done_still_no_ops_with_a_base(tmp_path, capsys):
    root, _, dash = _world(tmp_path)
    (tmp_path / "dash" / "pyproject.toml").write_text('[project]\nname = "dash"\nversion = "1.1.0"\n')
    steps = _land(_BRANCH)
    calls: list = []
    rc = cli._release_execute(steps, _V, root, {}, str(tmp_path / "coxswain"),
                              lambda argv, cwd: (calls.append(argv), (0, ""))[1], _MANIFEST, "")
    assert rc == 0
    assert calls == []
    assert capsys.readouterr().out.count("already at 1.1.0") == 5
