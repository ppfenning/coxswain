"""The release fetches each checkout first and refuses one whose HEAD is not origin/<default>."""
import subprocess
from types import SimpleNamespace

from devtools import cli, release

_MANIFEST_TOML = """
[coxswain]
version = "0.1.0"

[components.harness]
repo = "org/harness"
tag = "v0.1.0"
"""

_DIR = "/work/harness"


def _git(counts="0\t0\n", fetch=(0, ""), calls=None):
    """A fake runner answering every git call the gate makes; records argv into `calls`."""
    def run(argv, cwd):
        if calls is not None:
            calls.append(argv)
        if "fetch" in argv:
            return fetch
        if "status" in argv:
            return (0, "")
        if "rev-parse" in argv:
            return (0, "main\n")
        if "symbolic-ref" in argv:
            return (0, "refs/remotes/origin/main\n")
        if "--left-right" in argv:
            return (0, counts)
        return (0, "2\n")
    return run


def test_in_sync_is_none():
    assert cli.sync_refusal(0, 0, "origin/main") is None


def test_behind_names_the_count():
    assert cli.sync_refusal(0, 2, "origin/main") == "behind origin/main by 2 commits"


def test_ahead_names_the_count_and_is_singular_at_one():
    assert cli.sync_refusal(1, 0, "origin/main") == "ahead of origin/main by 1 commit"


def test_diverged_names_both_counts():
    assert cli.sync_refusal(1, 2, "origin/main") == "ahead of origin/main by 1 commit and behind by 2 commits"


def test_parse_counts_skips_noise_lines():
    assert cli.parse_counts("warning: refname is ambiguous\n2\t0\n") == (2, 0)


def test_parse_counts_is_none_without_a_pair_of_integers():
    assert cli.parse_counts("deadbeef\n3\n") is None


def test_stdout_run_drops_stderr(monkeypatch):
    done = SimpleNamespace(returncode=0, stdout="1\t0\n", stderr="warning: 9 9\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    assert cli._stdout_run(["git"], None) == (0, "1\t0\n")


def test_in_sync_checkout_is_fetched_first_and_ready():
    calls = []
    assert cli._checkout_ready(_DIR, _git(calls=calls)) == (True, "")
    assert calls[0] == release.fetch_argv(_DIR)


def test_behind_checkout_is_refused_with_the_count():
    assert cli._checkout_ready(_DIR, _git("0\t2\n")) == (False, f"{_DIR} is behind origin/main by 2 commits")


def test_ahead_checkout_is_refused_with_the_count():
    assert cli._checkout_ready(_DIR, _git("1\t0\n")) == (False, f"{_DIR} is ahead of origin/main by 1 commit")


def test_failed_fetch_is_a_refusal_naming_the_checkout():
    ready, reason = cli._checkout_ready(_DIR, _git(fetch=(128, "fatal: no remote\n")))
    assert (ready, reason) == (False, f"{_DIR}: fetch origin failed: fatal: no remote")


def test_unreadable_counts_refuse_rather_than_pass():
    ready, reason = cli._checkout_ready(_DIR, _git("deadbeef\n"))
    assert ready is False and "could not compare HEAD with origin/main" in reason


def test_stderr_from_fetch_and_the_count_runner_never_reads_as_a_count():
    # The fetch prints progress that looks like numbers; only the stdout-only count runner is parsed.
    run = _git(fetch=(0, "remote: Total 9 (delta 9)\n7 8\n"), counts="9 9\n")
    count_run = lambda argv, cwd: (0, "0\t0\n")  # noqa: E731
    assert cli._checkout_ready(_DIR, run, count_run=count_run) == (True, "")


def test_a_prefetched_checkout_is_not_fetched_again():
    calls = []
    assert cli._checkout_ready(_DIR, _git(calls=calls), fetched=True) == (True, "")
    assert not any("fetch" in argv for argv in calls)


def _dry_run(tmp_path, monkeypatch, capsys, counts):
    """Runs `release --dry-run`; returns (rc, stdout, ordered calls)."""
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text(_MANIFEST_TOML)
    calls = []
    monkeypatch.setattr(cli, "_maintainer_remote_url", lambda directory: "git@github.com:ppfenning/coxswain.git")
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: [])
    monkeypatch.setattr(cli, "_real_run", _git(counts, calls=calls))
    monkeypatch.setattr(cli, "_stdout_run", _git(counts, calls=calls))
    rc = cli.main(["release", "0.2.0", "--dry-run", "--manifest", str(manifest_path), "--root", str(tmp_path)])
    return rc, capsys.readouterr().out, calls


def test_every_checkout_is_fetched_before_the_plan_reads_commits(tmp_path, monkeypatch, capsys):
    rc, _, calls = _dry_run(tmp_path, monkeypatch, capsys, "0\t0\n")
    tag_counts = [i for i, argv in enumerate(calls) if "--count" in argv and "--left-right" not in argv]
    fetches = [i for i, argv in enumerate(calls) if "fetch" in argv]
    assert rc == 0
    assert len(fetches) == 2 and max(fetches) < min(tag_counts)


def test_pinned_commits_count_against_origin_default_not_head(tmp_path, monkeypatch, capsys):
    _, _, calls = _dry_run(tmp_path, monkeypatch, capsys, "0\t0\n")
    argv = next(a for a in calls if "--count" in a and "--left-right" not in a)
    assert argv[4] == "v0.1.0..origin/main"
    assert "HEAD" not in argv


def test_dry_run_prints_one_line_per_refused_checkout_and_exits_two(tmp_path, monkeypatch, capsys):
    rc, out, _ = _dry_run(tmp_path, monkeypatch, capsys, "0\t3\n")
    harness = release.component_dir(str(tmp_path), "harness", {})
    assert rc == 2
    assert f"refuse harness: {harness} is behind origin/main by 3 commits" in out
