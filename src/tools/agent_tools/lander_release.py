"""`cox lander release`: an operator's release of an initiative's merge hold, under the initiative's branch lease.

The release rewrites the landing record. The sweep, reconciliation and a phase merge rewrite the same record, each
under `landing_ops.branch_lease`, so the release takes that lease too: it never overwrites a record another writer
is midway through. The record is read once the lease is held, so what is released is the hold as it stands then.
What to release is decided by `lander_hold.release` (pure).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_tools import lander_hold
from harness import landing_ops as ops
from harness import landing_store as ls
from harness import store_lease

if TYPE_CHECKING:
    from agent_tools.lander import Ports

__all__ = ["release_merge_hold"]


def release_merge_hold(ports: Ports, initiative: str) -> tuple[bool, str]:
    """Edge. Release the initiative's merge hold for the next pass, whatever its cause or count. (released, what to
    say). Refused, with nothing written, while another worker holds the initiative's branch lease."""
    lease = ops.branch_lease(initiative)
    held = store_lease.acquire(ports.conn, lease, ports.worker, ports.now(), ops.CLAIM_SECONDS)
    if not held.ok:
        return False, (f"{held.holder or 'another worker'} is moving the branch of {initiative}; "
                       "release it again once that is done")
    try:
        record = ls.get_record(ports.conn, initiative)
        if record is None:
            return False, f"{initiative} has no landing record"
        released, said = lander_hold.release(record)
        if released is None:
            return False, said
        ls.put_record(ports.conn, {**released, "updated_at": ports.now()})
        return True, said
    finally:
        store_lease.release(ports.conn, lease, ports.worker, int(held.epoch or 0))
