"""A pulled issue's labels and milestone become the intake's stage, priority, epic and target release."""

import json
from types import SimpleNamespace

import pytest

from agent_tools import route, sources
from agent_tools.cli import main
from agent_tools.intake_pull_map import PullMap
from agent_tools.sources import Candidate


def _cand(link: str = "l1", *, repo: str = "a/b", labels=(), milestone: str = "") -> Candidate:
    return Candidate(title="Fix it", body="b", repo=repo, link=link, labels=frozenset(labels), milestone=milestone)


def _plan(cands, **kwargs):
    kwargs.setdefault("pull_map", PullMap())
    return route.pull_plan(cands, frozenset(), {"a/b": "tools"}, date="2026-11-16", source="github", **kwargs)


def _front(files: list[dict]) -> dict:
    [mapping] = files
    [text] = mapping.values()
    return route.parse_frontmatter(text)[0]


def test_a_ready_issue_with_milestone_priority_and_epic_carries_all_three():
    cand = _cand(labels={"intake:ready", "priority:3", "epic:regatta"}, milestone="2026.11.9")
    files, _, refusals = _plan([cand], known_epics={"regatta"})
    assert refusals == []
    front = _front(files)
    assert (front["stage"], front["priority"], front["epic"], front["target_release"]) == ("ready", "3", "regatta", "2026.11.9")
    assert files == [
        route.intake_file(
            "Fix it", "b", "tools", "2026-11-16", source="github", link="l1",
            stage="ready", epic="regatta", priority=3, target_release="2026.11.9",
        )
    ]


def test_the_same_issue_without_the_ready_label_is_a_draft():
    files, _, refusals = _plan([_cand(labels={"priority:3", "epic:regatta"})], known_epics={"regatta"})
    assert refusals == []
    assert _front(files)["stage"] == "draft"


def test_an_issue_with_no_labels_at_all_still_files_a_draft():
    files, _, refusals = _plan([_cand()])
    assert refusals == []
    front = _front(files)
    assert front["stage"] == "draft"
    assert not {"priority", "epic", "target_release"} & set(front)


def test_an_unknown_epic_is_refused_with_the_valid_choices_and_writes_nothing():
    files, _, refusals = _plan([_cand(labels={"intake:ready", "epic:nope"})], known_epics={"regatta", "tide"})
    assert files == []
    [line] = refusals
    assert "'Fix it' (l1)" in line
    assert "epic 'nope' is not known; valid epics: regatta, tide" in line


def test_an_epic_with_no_epics_known_is_refused():
    files, _, refusals = _plan([_cand(labels={"epic:regatta"})])
    assert files == []
    assert "no epic exists yet" in refusals[0]


def test_an_unmapped_repo_is_refused_with_the_valid_repos():
    files, _, refusals = _plan([_cand(repo="x/y")])
    assert files == []
    [line] = refusals
    assert "repo x/y has no mapping in the profile" in line
    assert "valid repos: a/b" in line


def test_a_custom_pull_map_is_honoured():
    custom = PullMap(ready_label="go", priority_prefix="p-", epic_prefix="e-", milestone_is_release=False)
    cand = _cand(labels={"go", "p-7", "e-regatta", "intake:ready"}, milestone="2026.11.9")
    files, _, refusals = _plan([cand], pull_map=custom, known_epics={"regatta"})
    assert refusals == []
    front = _front(files)
    assert (front["stage"], front["priority"], front["epic"]) == ("ready", "7", "regatta")
    assert "target_release" not in front


def test_with_no_pull_map_the_file_is_written_as_before_with_no_stage_line():
    files, _, refusals = _plan([_cand(labels={"intake:ready", "epic:nope"})], pull_map=None)
    assert refusals == []
    assert files == [route.intake_file("Fix it", "b", "tools", "2026-11-16", source="github", link="l1")]


def test_pull_notes_name_a_milestone_that_is_not_a_version():
    assert route.pull_notes([_cand(milestone="Someday"), _cand("l2", milestone="2026.11.9")], PullMap()) == [
        "routing: 'Fix it' (l1): milestone 'Someday' is not a version; target_release left empty"
    ]


def test_the_default_pull_map_reads_the_default_labels():
    files, _, _ = _plan([_cand(labels={"intake:ready"})])
    assert _front(files)["stage"] == "ready"


def test_the_profile_intake_map_parses_as_a_known_json_key():
    profile, unknown = route._parse_profile('intake_map: {"ready_label": "go", "milestone_is_release": false}\n')
    assert unknown == []
    assert profile["intake_map"] == {"ready_label": "go", "milestone_is_release": False}


def _pull_setup(tmp_path, monkeypatch, listing, extra_profile="", *, maps_labels=True):
    """A file-mode profile over one source whose adapter reads labels and milestone off each literal issue."""
    def read(raw):
        return Candidate(
            title=raw["title"], body=raw["body"], repo=raw["repo"], link=raw["link"],
            labels=frozenset(raw.get("labels", ())), milestone=raw.get("milestone", ""),
        )

    fake = SimpleNamespace(
        **{**vars(sources.FakeAdapter(listing)), "read": read},
        list_argv=lambda _config, repo: ["echo", json.dumps([r for r in listing if r["repo"] == repo])],
        **({"MAPS_LABELS": True} if maps_labels else {}),
    )
    monkeypatch.setattr(sources, "adapter_for", lambda _name: fake)
    ws = tmp_path / "workspace"
    ws.mkdir()
    (tmp_path / "cartridges" / "acme").mkdir(parents=True)
    (tmp_path / "cartridges" / "acme" / "cartridge.yaml").write_text("team: acme\n")
    widget = tmp_path / "repos" / "widget"
    (widget / ".git").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "team: acme\n"
        f"workspace_dir: {ws}\n"
        "harness_dir: /opt/coxswain-graphs\n"
        f"cartridges_dir: {tmp_path / 'cartridges'}\n"
        "provider_profile: /opt/providers/acme.yaml\n"
        'sources: {"github": {"repos": ["a/b"]}}\n'
        f'repo_map: {{"a/b": "{widget}"}}\n'
        f"{extra_profile}"
    )
    return profile, ws


def _issue(link, **extra):
    return {"title": "Fix it", "body": "b", "repo": "a/b", "link": link, **extra}


def test_the_pull_command_writes_the_mapped_fields_and_prints_a_milestone_note(tmp_path, monkeypatch, capsys):
    listing = [_issue("https://x/1", labels=["go", "p-2"], milestone="Someday")]
    profile, ws = _pull_setup(tmp_path, monkeypatch, listing, 'intake_map: {"ready_label": "go", "priority_prefix": "p-"}\n')
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    [written] = (ws / "intake").glob("*.md")
    front = route.parse_frontmatter(written.read_text(encoding="utf-8"))[0]
    assert (front["stage"], front["priority"]) == ("ready", "2")
    assert "milestone 'Someday' is not a version; target_release left empty" in capsys.readouterr().out


def test_the_pull_command_refuses_an_unknown_epic_and_writes_nothing(tmp_path, monkeypatch, capsys):
    profile, ws = _pull_setup(tmp_path, monkeypatch, [_issue("https://x/1", labels=["epic:nope"])])
    assert main(["route", "pull", "--profile", str(profile)]) == 2
    assert "epic 'nope' is not known" in capsys.readouterr().out
    assert not (ws / "intake").exists() or list((ws / "intake").glob("*.md")) == []


@pytest.mark.parametrize("bad", ['{"ready": "go"}', '{"milestone_is_release": "yes"}'])
def test_a_bad_intake_map_refuses_the_pull_naming_the_key(tmp_path, monkeypatch, capsys, bad):
    profile, ws = _pull_setup(tmp_path, monkeypatch, [_issue("https://x/1")], f"intake_map: {bad}\n")
    assert main(["route", "pull", "--profile", str(profile)]) == 2
    assert "routing: intake_map: " in capsys.readouterr().out
    assert not (ws / "intake").exists()


def test_a_source_that_maps_no_labels_writes_its_file_as_before(tmp_path, monkeypatch, capsys):
    listing = [_issue("https://x/1", labels=["epic:nope"], milestone="Someday")]
    profile, ws = _pull_setup(tmp_path, monkeypatch, listing, maps_labels=False)
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    [written] = (ws / "intake").glob("*.md")
    assert "stage" not in route.parse_frontmatter(written.read_text(encoding="utf-8"))[0]
    assert "milestone" not in capsys.readouterr().out


def test_the_github_adapter_maps_labels_and_the_folder_adapter_does_not():
    from agent_tools import source_folder, source_github

    assert source_github.MAPS_LABELS is True
    assert not getattr(source_folder, "MAPS_LABELS", False)
