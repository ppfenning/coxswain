import pytest

from agent_tools import queue_export, route, run_store, sources
from agent_tools.cli import main

ISSUE = "https://github.com/acme/widget/issues/7"
LINK_KEYS = ("link", "issue", "issue_repo")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A workspace, a profile with one fake pull source, and an in-memory store; no tracker is called."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    repo = tmp_path / "widget"
    (repo / ".git").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "team: acme\n"
        f"workspace_dir: {ws}\n"
        "harness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\n"
        "provider_profile: /opt/providers/acme.yaml\n"
        'sources: {"fake": {"repos": ["acme/widget"]}}\n'
        'repo_map: {"acme/widget": "widget"}\n'
    )
    rows: dict = {}

    def upsert(_runs_dir, row):
        rows[(row["initiative"], row["task_id"])] = row
        return True

    monkeypatch.setattr(run_store, "upsert_row", upsert)
    # The pull's source sync writes through the detail form: "" says the store took the row.

    def upsert_detail(_runs_dir, row):
        key = (row["initiative"], row["task_id"])
        rows[key] = {**rows.get(key, {}), **row}
        return ""

    monkeypatch.setattr(run_store, "upsert_row_detail", upsert_detail)
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: list(rows.values()))
    monkeypatch.setattr(run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(run_store, "resolve_initiative", lambda runs_dir, token: token)
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)
    listing = [{"link": ISSUE, "repo": "acme/widget", "title": "Pulled issue", "body": "b"}]
    monkeypatch.setattr(sources, "adapter_for", lambda name: sources.FakeAdapter(listing))
    monkeypatch.setattr("agent_tools.cli._fetch_listing", lambda adapter, config: list(listing))
    return {"ws": ws, "profile": str(profile), "repo": str(repo), "rows": rows}


def _file(env, *extra):
    return main([
        "route", "file", "--profile", env["profile"], "--repo", env["repo"], "--title", "Fix the thing", *extra,
    ])


def _edit(env, task_id, *extra):
    return main(["route", "edit", task_id, "--profile", env["profile"], *extra])


def _intake_row(env):
    (row,) = env["rows"].values()
    return row


def _file_fields(env, row):
    return route.parse_frontmatter((env["ws"] / queue_export.row_path(row)).read_text(encoding="utf-8"))[0]


def _no_new_intake_on_pull(env):
    # The store is the board, so the pull's sync may refresh the linked row's text from the source; what must hold
    # is that the pull files no second intake: the same files and the same rows, by key.
    before = (sorted((env["ws"] / "intake").glob("*.md")), sorted(env["rows"]))
    assert main(["route", "pull", "--profile", env["profile"], "--source", "fake"]) == 0
    assert (sorted((env["ws"] / "intake").glob("*.md")), sorted(env["rows"])) == before


def test_filing_an_intake_with_a_link_writes_the_row_and_the_file_alike(env):
    assert _file(env, "--intake", "--link", ISSUE) == 0
    row = _intake_row(env)
    assert [row["extra"].get(key) for key in LINK_KEYS] == [ISSUE, "7", "acme/widget"]
    fields = _file_fields(env, row)
    assert [str(fields.get(key)) for key in LINK_KEYS] == [ISSUE, "7", "acme/widget"]
    path = queue_export.row_path(row)
    on_disk = (env["ws"] / path).read_text(encoding="utf-8")
    # The filed text and the export render the same row in a different key order and quoting, so compare by meaning:
    # the file parses to the row, and the export of that parse is the export of the stored row.
    assert queue_export.own_export(path, on_disk, row) == queue_export.export_rows([row])[path]
    (env["ws"] / "runs" / ".export-baseline.json").unlink()  # the held verdict must not lean on the baseline record
    queue_export.export_files(env["ws"], [row], hashes=queue_export.Hashes({}, lambda *_a: True))
    assert queue_export.read_held_record(env["ws"]) == []
    assert queue_export.read_held(env["ws"], [row]) == []
    _no_new_intake_on_pull(env)


def test_editing_an_intake_sets_then_clears_the_link_in_the_row_and_the_file(env):
    assert _file(env, "--intake") == 0
    task_id = _intake_row(env)["task_id"]
    assert _edit(env, task_id, "--link", ISSUE) == 0
    row = _intake_row(env)
    assert [row["extra"].get(key) for key in LINK_KEYS] == [ISSUE, "7", "acme/widget"]
    assert [str(_file_fields(env, row).get(key)) for key in LINK_KEYS] == [ISSUE, "7", "acme/widget"]
    _no_new_intake_on_pull(env)

    assert _edit(env, task_id, "--link", "") == 0
    row = _intake_row(env)
    assert not any(key in row["extra"] for key in LINK_KEYS)
    assert not any(key in _file_fields(env, row) for key in LINK_KEYS)


def test_pull_does_file_the_issue_when_the_intake_carries_no_link(env):
    assert _file(env, "--intake") == 0
    main(["route", "pull", "--profile", env["profile"], "--source", "fake"])
    assert len(list((env["ws"] / "intake").glob("*.md"))) == 2


def test_editing_with_the_link_option_absent_leaves_a_link_alone(env):
    assert _file(env, "--intake", "--link", ISSUE) == 0
    task_id = _intake_row(env)["task_id"]
    assert _edit(env, task_id, "--title", "Fix the other thing") == 0
    assert _intake_row(env)["extra"]["link"] == ISSUE


def test_link_without_intake_is_refused_and_writes_nothing(env, capsys):
    assert _file(env, "--link", ISSUE) == 2
    assert "--link needs --intake" in capsys.readouterr().out
    assert env["rows"] == {}
    assert not (env["ws"] / "intake").exists()
    assert not (env["ws"] / "work").exists()
