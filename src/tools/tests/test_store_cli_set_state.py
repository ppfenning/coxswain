from agent_tools import run_store, store_cli
from agent_tools.store_cli import NotAvailable, StateRefused, StateSet


def _seed(tmp_path, state="review"):
    assert run_store.upsert_row(tmp_path, {"kind": "task", "initiative": "init", "task_id": "t1", "phase": "p1",
                                           "state": state, "needs": [], "surfaces": [], "title": "T", "body": ""})


def test_set_state_records_the_state_and_mirror_state_is_quiet(tmp_path):
    _seed(tmp_path)
    assert isinstance(store_cli.set_state(tmp_path, "init", "t1", "done", "me"), StateSet)
    assert store_cli.mirror_state(tmp_path, "init", "t1", "done", "me") is None


def test_a_missed_expectation_is_a_refusal_carrying_the_current_state(tmp_path):
    _seed(tmp_path, "review")
    result = store_cli.set_state(tmp_path, "init", "t1", "done", "me", expected="approved")
    assert isinstance(result, StateRefused)
    assert result.current == "review"


def test_mirror_state_turns_a_raise_into_a_warning(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(store_cli, "set_state", boom)
    assert store_cli.mirror_state(tmp_path, "init", "t1", "done", "me") == (
        "warning: store did not record init/t1 as done: RuntimeError: kaboom")


def test_a_store_that_could_not_be_opened_is_a_failure_and_one_with_no_reason_is_not():
    failed = store_cli.state_write_failure("init", "t1", "quarantined", NotAvailable("cannot open the store: x"))
    assert failed == "warning: store did not record init/t1 as quarantined: cannot open the store: x"
    assert store_cli.state_write_failure("init", "t1", "quarantined", NotAvailable()) is None
    assert store_cli.state_write_failure("init", "t1", "quarantined", StateSet({"state": "quarantined"})) is None


def test_reply_row_is_the_printed_row_on_exit_0_and_the_store_error_otherwise():
    assert store_cli.reply_row(0, '{"name": "shed"}') == {"name": "shed"}
    assert store_cli.reply_row(0, "not json") == {}
    assert store_cli.reply_row(3, "{}\n") == {store_cli.STORE_ERROR: "store_cli exit 3: {}"}
    assert store_cli.reply_row(2, "usage: ...\ninvalid choice") == {store_cli.STORE_ERROR: "store_cli exit 2: usage: ... invalid choice"}
