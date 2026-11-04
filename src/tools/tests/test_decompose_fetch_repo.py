from agent_tools import cli, route, run_store, store_fill

RUN = "demo-1"
HOST = cli.lane_hosts.LaneHost(name="box", ssh="me@box", workspace_dir="/host")
ENDED = "2026-11-03T05:00:00Z"
REPOLESS = "---\nid: demo\ntitle: Demo\nintake: intake/demo.md\n---\nwhy\n"


def _intake(repo: str) -> str:
    return "---\nid: demo\ntitle: Demo\n" + (f"repo: {repo}\n" if repo else "") + "---\nidea\n"


def _world(tmp_path, monkeypatch, *, pulled: str, intake: str, stored: list[dict] | None = None):
    """A chair workspace holding the intake; `run` writes `pulled` in place of the rsync; the store is a dict in memory."""
    ws = tmp_path / "chair"
    (ws / "intake").mkdir(parents=True)
    (ws / "intake" / "demo.md").write_text(intake)
    store = {(r["initiative"], r["task_id"]): r for r in stored or []}

    def run(argv):
        if argv[-1].endswith("/work/demo/"):
            (ws / "work" / "demo").mkdir(parents=True, exist_ok=True)
            (ws / "work" / "demo" / "initiative.md").write_text(pulled)
        return 0

    monkeypatch.setattr(store_fill, "_read_store", lambda runs_dir: list(store.values()))
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: store.__setitem__((row["initiative"], row["task_id"]), row) or "")
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: [
        r for r in store.values() if initiative in (None, r["initiative"]) and kind in (None, r["kind"])])
    return ws, run, store


def _fetch(ws, run):
    return cli._fetch_decompose(ws / "runs", HOST, "demo", RUN, True, ENDED, run)


def test_a_repoless_pull_gets_the_intake_repo_on_the_file_and_the_row(tmp_path, monkeypatch):
    ws, run, store = _world(tmp_path, monkeypatch, pulled=REPOLESS, intake=_intake("~/repos/widget"))
    outcome, _, _ = _fetch(ws, run)
    text = (ws / "work" / "demo" / "initiative.md").read_text()
    assert outcome == "fetched"
    assert route.parse_frontmatter(text)[0]["repo"] == "~/repos/widget"
    assert run_store.read_queue(ws / "runs", "demo", kind="initiative")[0]["extra"]["repo"] == "~/repos/widget"


def test_an_existing_row_without_a_repo_gets_extra_repo_from_the_fetch(tmp_path, monkeypatch):
    held = {"kind": "initiative", "initiative": "demo", "task_id": "demo", "state": "ready", "extra": {"intake": "intake/demo.md"}}
    ws, run, store = _world(tmp_path, monkeypatch, pulled=REPOLESS, intake=_intake("~/repos/widget"), stored=[held])
    assert _fetch(ws, run)[0] == "fetched"
    assert store[("demo", "demo")]["extra"] == {"intake": "intake/demo.md", "repo": "~/repos/widget"}


def test_a_pulled_repo_is_left_alone(tmp_path, monkeypatch):
    pulled = REPOLESS.replace("intake:", "repo: ~/repos/other\nintake:")
    ws, run, _ = _world(tmp_path, monkeypatch, pulled=pulled, intake=_intake("~/repos/widget"))
    assert _fetch(ws, run)[0] == "fetched"
    assert (ws / "work" / "demo" / "initiative.md").read_text() == pulled


def test_an_intake_without_a_repo_still_fetches(tmp_path, monkeypatch):
    ws, run, store = _world(tmp_path, monkeypatch, pulled=REPOLESS, intake=_intake(""))
    assert _fetch(ws, run)[0] == "fetched"
    assert (ws / "work" / "demo" / "initiative.md").read_text() == REPOLESS
    assert "repo" not in store[("demo", "demo")]["extra"]


def test_an_unreadable_intake_still_fetches(tmp_path, monkeypatch):
    ws, run, _ = _world(tmp_path, monkeypatch, pulled=REPOLESS, intake=_intake("~/repos/widget"))
    (ws / "intake" / "demo.md").unlink()
    assert _fetch(ws, run)[0] == "fetched"
    assert (ws / "work" / "demo" / "initiative.md").read_text() == REPOLESS
