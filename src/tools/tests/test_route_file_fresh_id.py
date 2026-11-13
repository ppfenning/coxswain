import pytest

from agent_tools import cli, route, run_store

_SLUG = route.slugify("Fix the thing")
_DROPPED = {"kind": "initiative", "task_id": _SLUG, "state": "dropped"}
_LIVE = {"kind": "initiative", "task_id": _SLUG, "state": "todo"}
_NOTE = f"initiative {_SLUG} is dropped, so the next decompose uses {_SLUG}-2"


@pytest.fixture(autouse=True)
def _no_gh_sync_and_store_writes(monkeypatch):
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: True)


def _profile(tmp_path, work_state):
    ws = tmp_path / "workspace"
    ws.mkdir()
    provider = tmp_path / "provider.yaml"
    provider.write_text(f"work_state: {work_state}\n")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"team: acme\nworkspace_dir: {ws}\nharness_dir: /opt/h\ncartridges_dir: /opt/c\nprovider_profile: {provider}\n"
    )
    repo = tmp_path / "widget"
    (repo / ".git").mkdir(parents=True)
    return profile, repo, ws


def _file_intake(tmp_path, monkeypatch, rows, work_state="store"):
    profile, repo, ws = _profile(tmp_path, work_state)
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: rows)
    rc = cli.main(
        ["route", "file", "--profile", str(profile), "--repo", str(repo), "--title", "Fix the thing", "--intake"]
    )
    return rc, ws


def test_filing_an_intake_whose_slug_matches_a_dropped_initiative_names_the_fresh_id(tmp_path, monkeypatch, capsys):
    rc, ws = _file_intake(tmp_path, monkeypatch, [_DROPPED])
    assert rc == 0
    assert _NOTE in capsys.readouterr().out.splitlines()
    (filed,) = (ws / "intake").iterdir()
    assert filed.name.endswith(f"-{_SLUG}.md")
    assert f"id: {_SLUG}\n" in filed.read_text()


@pytest.mark.parametrize("rows", [[], [_LIVE], [{"kind": "initiative", "task_id": "other", "state": "dropped"}]])
def test_filing_an_intake_with_no_dropped_match_prints_no_note(tmp_path, monkeypatch, capsys, rows):
    rc, _ = _file_intake(tmp_path, monkeypatch, rows)
    assert rc == 0
    assert "the next decompose uses" not in capsys.readouterr().out


_FRESH = {"kind": "initiative", "task_id": f"{_SLUG}-2", "state": "todo"}
_FRESH_DROPPED = {**_FRESH, "state": "dropped"}


def _chair_reads(tmp_path, monkeypatch, rows, initiative="cited"):
    """The run ids the chair reads for an empty initiative named `initiative` that cites an intake with id `_SLUG`."""
    (tmp_path / "work" / initiative / "p1").mkdir(parents=True)
    (tmp_path / "work" / initiative / "initiative.md").write_text("---\nintake: cited.md\n---\n", encoding="utf-8")
    (tmp_path / "cited.md").write_text(f"---\nid: {_SLUG}\n---\nbody\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: rows)
    monkeypatch.setattr(run_store, "work_items", lambda runs_dir, initiative=None, cache=None: [])
    monkeypatch.setattr(
        cli.chair_decompose_runs, "read_decompose_runs", lambda runs_dir, iid, cache=None: seen.append(iid) or []
    )
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "store")
    deps.facts_deps.decomposed_intake()  # type: ignore[misc]
    return seen


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([_DROPPED], f"{_SLUG}-2"),
        ([_DROPPED, _FRESH], f"{_SLUG}-2"),  # the board after the launch made the fresh initiative
        ([_DROPPED, _FRESH_DROPPED], f"{_SLUG}-3"),
        ([_LIVE], _SLUG),
        ([], _SLUG),
    ],
)
def test_the_chair_reads_decompose_runs_under_the_fresh_id(tmp_path, monkeypatch, rows, expected):
    assert _chair_reads(tmp_path, monkeypatch, rows) == [expected]


def test_the_dropped_initiative_keeps_reading_its_own_runs(tmp_path, monkeypatch):
    assert _chair_reads(tmp_path, monkeypatch, [_DROPPED, _FRESH], initiative=_SLUG) == [_SLUG]
