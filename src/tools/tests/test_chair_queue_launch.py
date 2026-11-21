"""C4: a decompose launch a queue worker makes goes past the loop guard beside the tick's live lock only while the store
says the worker's `chair:decompose` lease is held, by its holder at its epoch; a lost or stale lease is refused.

The rule is pure (`chair_kinds.launch_refusal`); the lease is a real one in the sqlite store, and the guard is the
command's own (`cli_land._leader_guard_or_refuse`) over a real live lock written to the runs directory."""

import argparse
import datetime
import os
import socket
from pathlib import Path

import pytest

from agent_tools import (  # noqa: F401  (cli: its import loads the command table)
    chair,
    chair_kinds,
    chair_read_queues,
    cli,
    run_store,
)
from agent_tools.chair_exec import Deps, perform
from agent_tools.cli_land import _leader_guard_or_refuse
from agent_tools.cli_route_launch import _queue_proof
from harness import store_chair_lease as kl
from harness.store_migrate import open_store

REFUSED = "refusing: the landing loop is held by tick (live); pass --force to override"
PROOF = chair_kinds.QueueProof("chair:decompose", "decompose-w", 3)


def test_the_lock_letting_a_launch_through_needs_no_proof():
    assert chair_kinds.launch_refusal(None, "decompose", None, held=False) is None
    assert chair_kinds.launch_refusal(None, "decompose", PROOF, held=False) is None


def test_a_launch_the_lock_refuses_goes_on_only_under_a_held_lease_of_its_own_graph():
    assert chair_kinds.launch_refusal(REFUSED, "decompose", None, held=True) == REFUSED
    assert chair_kinds.launch_refusal(REFUSED, "decompose", PROOF, held=True) is None
    lost = chair_kinds.launch_refusal(REFUSED, "decompose", PROOF, held=False)
    assert lost == f"{REFUSED}; chair:decompose is not held by decompose-w at epoch 3 by the store's clock"
    other = chair_kinds.QueueProof("chair:watch", "watch-w", 3)
    assert chair_kinds.launch_refusal(REFUSED, "decompose", other, held=True) == f"{REFUSED}; chair:watch does not cover a decompose launch"
    assert chair_kinds.launch_refusal(REFUSED, "epic", PROOF, held=True) == f"{REFUSED}; chair:decompose does not cover a epic launch"


def test_the_proof_rides_as_three_flags_and_reads_back_only_whole():
    argv = chair_kinds.proof_argv(PROOF)
    assert argv == ["--queue-lease", "chair:decompose", "--queue-holder", "decompose-w", "--queue-epoch", "3"]
    whole = argparse.Namespace(queue_lease="chair:decompose", queue_holder="decompose-w", queue_epoch=3)
    assert _queue_proof(whole) == (PROOF, "")
    assert _queue_proof(argparse.Namespace()) == (None, "")
    partial = argparse.Namespace(queue_lease="chair:decompose", queue_holder=None, queue_epoch=3)
    assert _queue_proof(partial)[1] == "routing: --queue-lease, --queue-holder and --queue-epoch go together"


def _runs(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    open_store(run_store.store_url_for(runs), "2026-11-20T00:00:00Z").close()
    return runs


def _acquire(runs: Path, holder: str, ttl: int = 60) -> int:
    conn = open_store(run_store.store_url_for(runs), "2026-11-20T00:00:00Z")
    try:
        epoch = kl.acquire(conn, "chair:decompose", holder, ttl)
        assert epoch is not None
        return epoch
    finally:
        conn.close()


def test_the_store_answers_for_the_holder_at_its_epoch_only(tmp_path: Path):
    runs = _runs(tmp_path)
    epoch = _acquire(runs, "decompose-w")
    assert chair_read_queues.queue_lease_holds(runs, chair_kinds.QueueProof("chair:decompose", "decompose-w", epoch))
    assert not chair_read_queues.queue_lease_holds(runs, chair_kinds.QueueProof("chair:decompose", "decompose-w", epoch + 1))
    assert not chair_read_queues.queue_lease_holds(runs, chair_kinds.QueueProof("chair:decompose", "someone-else", epoch))
    assert not chair_read_queues.queue_lease_holds(runs, chair_kinds.QueueProof("chair:watch", "decompose-w", epoch))


def test_a_broken_store_reads_as_not_held(tmp_path: Path):
    runs = _runs(tmp_path)
    store = run_store.sqlite_path(run_store.store_url_for(runs))
    for suffix in ("", "-wal", "-shm"):
        Path(f"{store}{suffix}").unlink(missing_ok=True)
    Path(store).write_text("not a database", encoding="utf-8")
    assert not chair_read_queues.queue_lease_holds(runs, PROOF)


def _live_tick_lock(runs: Path) -> None:
    """The tick's own live lock, under its label, held by this live process on this host."""
    record, _ = chair.take(None, "tick", os.getpid(), socket.gethostname(), datetime.datetime.now(datetime.UTC), 30, True)
    assert record is not None
    chair.write(runs, record)


def test_beside_the_ticks_live_lock_a_worker_launch_goes_on_under_its_held_lease(tmp_path: Path, capsys):
    runs = _runs(tmp_path)
    _live_tick_lock(runs)
    epoch = _acquire(runs, "decompose-w")
    held = chair_kinds.QueueProof("chair:decompose", "decompose-w", epoch)
    assert _leader_guard_or_refuse(runs, "worker", False, explicit_label="worker", queue_proof=("decompose", held)) is None
    assert "decompose launch under chair:decompose at epoch" in capsys.readouterr().out
    assert chair.read(runs)["session"] == "tick"  # the lock is the tick's still: the launch never claims it


@pytest.mark.parametrize("why", ["no proof", "stale epoch", "lease lost"])
def test_beside_the_ticks_live_lock_a_worker_launch_without_a_held_lease_is_refused(tmp_path: Path, capsys, why: str):
    runs = _runs(tmp_path)
    _live_tick_lock(runs)
    epoch = _acquire(runs, "decompose-w")
    if why == "lease lost":  # another worker took the lease over; the first one's epoch is now stale
        conn = open_store(run_store.store_url_for(runs), "2026-11-20T00:00:00Z")
        try:
            assert kl.release(conn, "chair:decompose", "decompose-w", epoch)
        finally:
            conn.close()
        _acquire(runs, "decompose-x")
    proof = None if why == "no proof" else chair_kinds.QueueProof("chair:decompose", "decompose-w", epoch - (why == "stale epoch"))
    rc = _leader_guard_or_refuse(runs, "worker", False, explicit_label="worker", queue_proof=("decompose", proof) if proof else None)
    assert rc == 2
    out = capsys.readouterr().out
    assert out.startswith("refusing: the landing loop is held by tick (live)")
    assert ("is not held by decompose-w" in out) == (why != "no proof")


def _launch_deps(argvs: list, proof: chair_kinds.QueueProof | None) -> Deps:
    def run(argv):
        argvs.append(argv)
        return 0, "ok"

    return Deps(
        run=run, delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=lambda action: None, run_id=lambda action: "run-1", repo_for=lambda action: "r",
        intake_id=lambda path: "x", initiative_rows=lambda: [], queue_proof=lambda: proof,
    )


def test_a_workers_decompose_launch_carries_its_lease_and_one_with_no_lease_is_fenced_unsent():
    action = {"kind": "launch_decompose", "intake_ids": ["intake/x.md"], "epoch": 1}
    argvs: list = []
    perform([action], _launch_deps(argvs, PROOF), lambda: 1, False)  # type: ignore[list-item]
    assert argvs[0][-6:] == chair_kinds.proof_argv(PROOF)
    argvs.clear()
    results = perform([action], _launch_deps(argvs, None), lambda: 1, False)  # type: ignore[list-item]
    assert argvs == [] and results[0]["status"] == "fenced"


def test_the_ticks_own_launches_carry_no_proof():
    action = {"kind": "launch_decompose", "intake_ids": ["intake/x.md"], "epoch": 1}
    argvs: list = []
    deps = _launch_deps(argvs, PROOF)
    perform([action], Deps(**{**deps.__dict__, "queue_proof": None}), lambda: 1, False)  # type: ignore[list-item]
    assert "--queue-lease" not in argvs[0]
