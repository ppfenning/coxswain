import pytest

from devtools import cli, release

_START = "feature/x"
_INDEX = {"urls": [{"packagetype": "sdist", "url": "https://x/cox-0.2.0.tar.gz", "digests": {"sha256": "b" * 64}}]}
_STEP = {"kind": "tap_formula_pr", "component": "tap", "index_url": "https://x/index.json",
         "path": "Formula/cox.rb", "branch": "release/0.2.0", "title": "cox 0.2.0"}


def _fake_run(directory, fail_on=None):
    """A git/gh runner that tracks the tap clone's branch; `fail_on(argv)` picks the one call that returns 1."""
    calls: list = []
    state = {"branch": _START}

    def run(argv, cwd):
        calls.append(argv)
        if fail_on is not None and fail_on(argv):
            return 1, "refused"
        if argv[0] == "gh":
            return 0, ""
        if argv[3] == "checkout":
            state["branch"] = argv[-1]
        if argv[3] == "rev-parse":
            return 0, state["branch"] + "\n"
        if argv[3] == "symbolic-ref":
            return 0, "refs/remotes/origin/main\n"
        return 0, ""

    return calls, state, run


def _execute(tmp_path, fail_on=None, fetch_index=lambda url: _INDEX, sleeps=None):
    directory = tmp_path / release.TAP_CHECKOUT
    (directory / "Formula").mkdir(parents=True)
    (directory / "Formula" / "cox.rb").write_text("formula\n")
    calls, state, run = _fake_run(str(directory), fail_on)
    rc = cli._release_execute([_STEP], "0.2.0", str(tmp_path), {}, str(tmp_path), run, {}, "",
                              sleep=(sleeps.append if sleeps is not None else lambda s: None),
                              now=iter(range(0, 10**9, 60)).__next__,
                              fetch_index=fetch_index, fetch_text=lambda url: "a" * 64 + "  asset\n")
    return rc, calls, state


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyError("anchor"), ValueError("no anchor")])
def test_a_write_exception_is_a_failed_step_and_restores_the_starting_branch(tmp_path, monkeypatch, capsys, error):
    def raising(*args, **kwargs):
        raise error

    monkeypatch.setattr(release, "bumped_formula_text", raising)
    rc, calls, state = _execute(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2
    assert "FAILED tap_formula_pr tap: " in out and str(error) in out
    assert any(c[3:5] == ["checkout", "-b"] for c in calls if c[0] == "git")
    assert state["branch"] == _START


def test_a_push_failure_is_a_failed_step_and_restores_the_starting_branch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(release, "bumped_formula_text", lambda text, *a, **k: text)
    rc, _, state = _execute(tmp_path, fail_on=lambda argv: argv[0] == "git" and argv[3] == "push")
    assert rc == 2
    assert "FAILED tap_formula_pr tap: refused" in capsys.readouterr().out
    assert state["branch"] == _START


def test_a_pr_create_failure_is_a_failed_step_and_restores_the_starting_branch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(release, "bumped_formula_text", lambda text, *a, **k: text)
    rc, calls, state = _execute(tmp_path, fail_on=lambda argv: argv[:3] == ["gh", "pr", "create"])
    assert rc == 2
    assert "FAILED tap_formula_pr tap: refused" in capsys.readouterr().out
    assert next(c for c in calls if c[0] == "gh")[:3] == ["gh", "pr", "create"]
    assert state["branch"] == _START


def test_fetch_index_succeeds_on_the_third_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "bumped_formula_text", lambda text, *a, **k: text)
    attempts, sleeps = [], []

    def flaky(url):
        attempts.append(url)
        if len(attempts) < 3:
            raise OSError("HTTP Error 503")
        return _INDEX

    rc, calls, _ = _execute(tmp_path, fail_on=lambda argv: argv[:3] == ["gh", "pr", "create"], fetch_index=flaky, sleeps=sleeps)
    assert len(attempts) == 3
    assert sleeps == [30.0, 30.0]
    assert release.fetch_argv(str(tmp_path / release.TAP_CHECKOUT)) in calls
    assert rc == 2


def test_fetch_index_gives_up_after_the_last_attempt(tmp_path, capsys):
    attempts, sleeps = [], []

    def down(url):
        attempts.append(url)
        raise OSError("HTTP Error 503")

    rc, calls, _ = _execute(tmp_path, fetch_index=down, sleeps=sleeps)
    assert rc == 2
    assert len(attempts) == 5
    assert sleeps == [30.0] * 4
    assert "FAILED tap_formula_pr tap: https://x/index.json: HTTP Error 503" in capsys.readouterr().out
    assert calls == []
