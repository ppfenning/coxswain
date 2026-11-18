
from agent_tools import land, store_cli

APPROVED = "---\nid: t1\nstate: approved\n---\nbody\n"
DONE = "---\nid: t1\nstate: done\n---\nbody\n"


def test_store_mode_reads_the_store_state():
    assert land.approved_state("store", "approved", "ready") == "approved"


def test_store_mode_without_a_row_reads_the_file_state():
    assert land.approved_state("store", None, "approved") == "approved"


def test_files_mode_ignores_the_store():
    assert land.approved_state("files", "done", "approved") == "approved"


def test_a_set_state_success_continues():
    assert land.set_state_stop(store_cli.StateSet({"state": "done"})) is None


def test_a_refused_precondition_names_another_machine():
    reason = land.set_state_stop(store_cli.StateRefused("state is done", "done"))
    assert "another machine landed this task" in reason


def test_a_failure_stops_with_its_exit_and_detail():
    assert land.set_state_stop(store_cli.Failed(2, "boom")) == "land: store set-state failed: exit 2: boom"


def test_not_available_stops():
    assert "store not available" in land.set_state_stop(store_cli.NotAvailable())


def test_a_merged_approved_item_moves_to_done():
    assert land.approve_to_done(APPROVED, merged=True) == (DONE, None)
