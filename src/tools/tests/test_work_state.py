"""The store is the only board. `files` mode, in which the ticket files were the board, is retired."""

from agent_tools import work_state

PG = "postgresql://db.example/cox"


def _refuse_probe(url):
    raise AssertionError("the mode never depends on a probe")


def test_the_mode_is_store_whatever_the_profile_says(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", _refuse_probe)
    for profile in ({}, {"work_state": "store"}, {"work_state": "files"}, {"work_state": None}, {"work_state": "sotre"},
                    {"storage_url": PG}, {"work_state": "files", "storage_url": PG}):
        assert work_state.work_state_mode(profile) == "store"
    assert work_state.work_state_mode_at("no-such-profile.yaml") == "store"


def test_a_store_that_answers_gives_the_plain_status_line(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", lambda url: True)
    assert work_state.resolve({"storage_url": PG}) == ("store", "work state: store")


def test_a_store_that_does_not_answer_is_still_the_board_and_the_line_says_so(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", lambda url: False)
    assert work_state.resolve({"work_state": "store", "storage_url": PG}) == ("store", "work state: store (unreachable)")
    assert work_state.resolve({"storage_url": PG}) == ("store", "work state: store (unreachable)"), "nothing stands in for the store"


def test_a_profile_that_still_says_files_is_told_the_key_is_retired(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", _refuse_probe)
    mode, line = work_state.resolve({"work_state": "files", "storage_url": PG})
    assert mode == "store" and "work_state: files is retired" in line and "Remove the key" in line


def test_a_store_url_that_is_not_postgres_is_never_probed(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", _refuse_probe)
    assert work_state.resolve({"storage_url": "sqlite:///runs/cox.db"}) == ("store", "work state: store")
    assert work_state.resolve({"storage_url": "not a url"}) == ("store", "work state: store")


def test_the_probe_adds_a_connect_timeout_unless_the_url_sets_one():
    assert work_state._with_connect_timeout(PG) == f"{PG}?connect_timeout=3"
    assert work_state._with_connect_timeout(f"{PG}?connect_timeout=9") == f"{PG}?connect_timeout=9"
    assert work_state._with_connect_timeout(f"{PG}?sslmode=require") == f"{PG}?sslmode=require&connect_timeout=3"
