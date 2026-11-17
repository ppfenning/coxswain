from agent_tools import store_cli
from agent_tools.store_cli import LeaseGranted, LeaseReleased


def test_land_lease_name():
    assert store_cli.land_lease_name("t1") == "land:t1"


def test_a_lease_is_acquired_renewed_and_released_in_process(tmp_path):
    assert store_cli.lease_acquire(tmp_path, "chair", "me", 60) == LeaseGranted(1, "me")
    assert store_cli.lease_renew(tmp_path, "chair", "me", 1, 60) == LeaseGranted(1, "me")
    assert store_cli.lease_release(tmp_path, "chair", "me", 1) == LeaseReleased()
    assert (tmp_path / "cox.db").is_file()


def test_a_held_lease_refuses_a_second_holder(tmp_path):
    assert store_cli.lease_acquire(tmp_path, "chair", "me", 60) == LeaseGranted(1, "me")
    assert isinstance(store_cli.lease_acquire(tmp_path, "chair", "you", 60), store_cli.LeaseRefused)
