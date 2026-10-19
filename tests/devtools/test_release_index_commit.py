import pytest
from test_release import V0, _fake_git_run, _maintainer_checkout, _stdout_runner_follows_the_fake, no_tap  # noqa: F401
from test_release import _cut as _cut_at_declared_version

from devtools import cli, release

_VERSION = "2026.10.0"
_MANIFEST = {"coxswain": {"version": _VERSION}, "components": {"dash": {"tag": "v2026.9.0"}}}
_STALE = "# Releases\n\n## 2026.9.0\n\nolder\n"


def _umbrella(tmp_path, index_text):
    releases = tmp_path / "docs" / "releases"
    releases.mkdir(parents=True)
    (releases / f"{_VERSION}.md").write_text("notes\n")
    (releases / "index.md").write_text(index_text)
    return tmp_path


def _steps(index_changed):
    plan = release.release_plan(_MANIFEST, _VERSION, {"coxswain": []}, index_changed=index_changed)
    return plan[: next(i for i, s in enumerate(plan) if s["kind"] == "tag_self") + 1]


def _execute(umbrella, steps):
    calls: list = []

    def run(argv, cwd):
        calls.append(argv)
        if argv[:1] == ["git"] and argv[3:5] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if argv[:1] == ["git"] and argv[3] == "rev-list":
            return 0, "0\t0\n"
        if argv[:1] == ["git"] and argv[3] == "symbolic-ref":
            return 1, ""
        return 0, ""

    rc = cli._release_execute(steps, _VERSION, str(umbrella), {}, str(umbrella), run, _MANIFEST, "",
                              sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__)
    return rc, calls


def _index_of(calls, *words):
    return next(i for i, c in enumerate(calls) if all(w in c for w in words))


def test_a_stale_index_is_branched_committed_pushed_and_merged_before_the_tag(tmp_path):
    umbrella = _umbrella(tmp_path, _STALE)
    rc, calls = _execute(umbrella, _steps(index_changed=True))
    branch = _index_of(calls, "checkout", "-b", "release/" + _VERSION)
    commit = _index_of(calls, "commit", "docs/releases/index.md")
    push = _index_of(calls, "push", "-u", "release/" + _VERSION)
    merge = _index_of(calls, "merge")
    tag = _index_of(calls, "tag", "v" + _VERSION)
    assert rc == 0
    assert branch < commit < push < merge < tag
    assert (umbrella / "docs" / "releases" / "index.md").read_text() == release.release_index_text(
        _STALE, _VERSION, _MANIFEST, tagged=set())


def test_a_matching_index_runs_no_branch_commit_or_push(tmp_path):
    current = release.release_index_text(_STALE, _VERSION, _MANIFEST, tagged=set())
    umbrella = _umbrella(tmp_path, current)
    rc, calls = _execute(umbrella, _steps(index_changed=False))
    assert rc == 0
    assert not [c for c in calls if "-b" in c or "commit" in c or "-u" in c or "merge" in c]
    assert [c[3:5] for c in calls if "push" in c] == [["push", "origin"]]
    assert (umbrella / "docs" / "releases" / "index.md").read_text() == current


def test_commit_index_with_nothing_rewritten_no_ops_the_landing_steps(tmp_path, capsys):
    umbrella = _umbrella(tmp_path, release.release_index_text(_STALE, _VERSION, _MANIFEST, tagged=set()))
    rc, calls = _execute(umbrella, _steps(index_changed=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "commit_index coxswain: nothing changed" in out
    assert "merge coxswain: already at " + _VERSION in out
    assert not [c for c in calls if "-b" in c or "commit" in c or "-u" in c or "merge" in c]


def test_a_commit_failure_is_a_failed_commit_index_step(tmp_path, capsys):
    umbrella = _umbrella(tmp_path, _STALE)
    steps = _steps(index_changed=True)
    calls: list = []

    def run(argv, cwd):
        calls.append(argv)
        if "commit" in argv:
            return 1, "hook refused"
        if argv[3:5] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if argv[3:4] == ["rev-list"]:
            return 0, "0\t0\n"
        return (1, "") if argv[3:4] == ["symbolic-ref"] else (0, "")

    rc = cli._release_execute(steps, _VERSION, str(umbrella), {}, str(umbrella), run, _MANIFEST, "",
                              sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__)
    assert rc == 2
    assert "FAILED commit_index coxswain: hook refused" in capsys.readouterr().out
    assert not [c for c in calls if "push" in c]


def test_the_dry_run_detail_names_the_file_and_branch():
    step = {"kind": "commit_index", "component": "coxswain", "branch": "release/x", "paths": ["docs/releases/index.md"]}
    assert cli._release_detail(step) == "docs/releases/index.md on release/x"


@pytest.mark.usefixtures("no_tap")
def test_a_cut_renders_the_index_with_the_components_it_tags_and_lands_it_before_the_tag(tmp_path, monkeypatch, capsys):
    rc, calls, umbrella = _cut_at_declared_version(tmp_path, monkeypatch, fake=_fake_git_run(commits=1))
    index = (umbrella / "docs" / "releases" / "index.md").read_text()
    commit = next(i for i, c in enumerate(calls) if c[3:4] == ["commit"] and "docs/releases/index.md" in c)
    tag = next(i for i, c in enumerate(calls) if c[3:4] == ["tag"] and c[2] == str(umbrella))
    assert rc == 0 and commit < tag
    assert f"v{V0}" in index.split("| dash |")[1].splitlines()[0]
    assert f"commit_index coxswain: docs/releases/index.md on release/{V0}" in capsys.readouterr().out


@pytest.mark.usefixtures("no_tap")
def test_a_dry_run_prints_the_commit_index_line_and_writes_nothing(tmp_path, monkeypatch, capsys):
    rc, calls, umbrella = _cut_at_declared_version(tmp_path, monkeypatch, V0, "--dry-run")
    assert rc == 0
    assert f"commit_index coxswain: docs/releases/index.md on release/{V0}" in capsys.readouterr().out
    assert not (umbrella / "docs" / "releases" / "index.md").exists()
