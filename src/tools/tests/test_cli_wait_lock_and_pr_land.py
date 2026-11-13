import pytest

# The fixtures and the `_land` driver are the store-mode land harness; this file scripts the repo lease on top of it.
from test_land_from_store import _land, _own_tempdir, repo, store  # noqa: F401

from agent_tools import cli, forge_github, pr_land, store_cli

HOLDER = "other@host:1"
URL = "https://github.com/o/r/pull/7"


@pytest.fixture
def repo_lease(store, monkeypatch):  # noqa: F811
    """Refuse the repo lease (`land:/...`) `refusals` times naming `HOLDER`, then fall through to the store fake."""
    calls = []
    granting = store_cli.lease_acquire

    def script(refusals):
        def acquire(runs_dir, name, holder, ttl, **kwargs):
            if not name.startswith("land:/"):
                return granting(runs_dir, name, holder, ttl)
            calls.append(name)
            return store_cli.LeaseRefused(4, HOLDER) if len(calls) <= refusals else granting(runs_dir, name, holder, ttl)

        monkeypatch.setattr(store_cli, "lease_acquire", acquire)
        monkeypatch.setattr(cli, "_LAND_LOCK_POLL_SECONDS", 0)
        return calls

    return script


def test_a_held_repo_lease_released_after_two_polls_lets_the_wait_flag_land(repo, tmp_path, monkeypatch, store, capsys, repo_lease):  # noqa: F811
    calls = repo_lease(3)
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--wait-lock",))
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert "mark_done" in ran
    assert len(calls) == 4
    assert lines.count(f"land: waiting, {HOLDER} is landing in {repo}") == 1
    assert not any("land: refusing" in line for line in lines)


def test_the_same_land_without_the_flag_prints_the_refusal_and_exits_2(repo, tmp_path, monkeypatch, store, capsys, repo_lease):  # noqa: F811
    calls = repo_lease(3)
    rc, ran = _land(repo, tmp_path, monkeypatch, store)
    out = capsys.readouterr().out
    assert (rc, ran, len(calls)) == (2, [], 1)
    assert f"land: refusing, {HOLDER} is landing in {repo}" in out
    assert "land: waiting" not in out


def test_a_lease_held_past_the_timeout_still_ends_in_the_refusal(repo, tmp_path, monkeypatch, store, capsys, repo_lease):  # noqa: F811
    calls = repo_lease(10**6)
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--wait-lock", "--wait-lock-timeout", "0"))
    out = capsys.readouterr().out
    assert (rc, ran) == (2, [])
    assert len(calls) == 2
    assert f"land: refusing, {HOLDER} is landing in {repo}" in out


def test_the_wait_lock_timeout_states_its_default_in_the_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["runs", "land", "--help"])
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps the help text
    assert exc.value.code == 0
    assert "--wait-lock-timeout" in out
    assert "(default 600)" in out


class Forge:
    """Recording forge edges for one open PR 7 whose required check is `test`."""

    def __init__(self, monkeypatch, tmp_path, rows, body="Task: t-one"):
        self.merges, self.marked = [], []
        pr = {"number": 7, "state": "OPEN", "url": URL, "headRefName": "b", "headRefOid": "abc", "baseRefName": "main",
              "body": body}
        monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path / "no-profile.yaml"))
        monkeypatch.setattr(cli, "_runs_dir_for_land", lambda _args: (tmp_path, None))
        monkeypatch.setattr(pr_land, "_store_mark_done",
                            lambda _runs, _at, task, url: (self.marked.append((task, url)), (True, f"{task} marked done"))[1])
        monkeypatch.setattr(forge_github, "read_pr", lambda _repo, number: pr)
        monkeypatch.setattr(forge_github, "required_for_pr", lambda _repo, number: ("test",))
        monkeypatch.setattr(forge_github, "pr_check_rows", lambda _repo, sha: rows)
        monkeypatch.setattr(forge_github, "merge_pr", lambda _repo, number: (self.merges.append(number), (True, "merged"))[1])


def _pr_land(tmp_path):
    return cli.main(["pr", "land", "7", "--repo", str(tmp_path), "--timeout", "0"])


@pytest.mark.parametrize("rows", [[{"name": "test", "state": "failure"}], [{"name": "lint", "state": "success"}]],
                         ids=["red", "missing"])
def test_pr_land_exits_2_with_merge_failed_and_never_merges_without_a_green_required_check(tmp_path, monkeypatch, capsys, rows):
    fake = Forge(monkeypatch, tmp_path, rows)
    rc = _pr_land(tmp_path)
    assert rc == 2
    assert "merge_failed" in capsys.readouterr().out
    assert fake.merges == []


def test_pr_land_exits_0_with_level_merged_and_merges_once_on_green(tmp_path, monkeypatch, capsys):
    fake = Forge(monkeypatch, tmp_path, [{"name": "test", "state": "success"}])
    rc = _pr_land(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert f"level_merged: merged {URL}; marked 1 task(s) done" in out
    assert (fake.merges, fake.marked) == ([7], [("t-one", URL)])


def test_pr_land_exits_2_and_never_merges_an_open_pr_that_names_no_tasks(tmp_path, monkeypatch, capsys):
    fake = Forge(monkeypatch, tmp_path, [{"name": "test", "state": "success"}], body="no carried tasks")
    rc = _pr_land(tmp_path)
    assert rc == 2
    assert "names no tasks" in capsys.readouterr().out
    assert (fake.merges, fake.marked) == ([], [])
