"""Wake on a store write: listen on the channel that migration 0021's triggers notify.

Each watched table's statement trigger (`store_ddl_0021`) sends `pg_notify('cox_store', <table>)`. Postgres delivers
it when the writer commits, once per distinct table in that transaction, and drops it if the writer rolls back. A
notification is a wake and nothing more. A listener that wakes re-reads the rows it cares about, and keeps polling on
a timer as its fallback, so a notification lost to a dropped connection costs latency, never a wrong decision.

`Listener.wait(timeout)` blocks until the first notification or the timeout, then takes whatever else has already
arrived, and returns the tables named (empty on a timeout). The queue workers (C3) and the event-driven chair use it
in place of a plain sleep. The connection runs in autocommit and never opens a transaction: a listener inside an open
transaction holds back the server's notification queue for every writer.

sqlite, the test double, has no NOTIFY: `listen` returns None there, and the caller keeps its plain sleep.

    python -m harness.store_notify --store-url URL [--seconds N]

prints one line per notification, `<utc time> <table>`, for N seconds (default 60). It is for watching the triggers
during the soak before the cutover.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from harness.store_ddl_0021 import CHANNEL
from harness.store_dialect import POSTGRES, connect
from harness.traces_url import redact_url

__all__ = ["CHANNEL", "Listener", "listen", "main", "tables"]


class _Notify(Protocol):
    channel: str
    payload: str


def tables(notifies: Iterable[_Notify], channel: str = CHANNEL) -> frozenset[str]:
    """Pure. The distinct tables named by the notifications on `channel`. Others on the same connection are ignored."""
    return frozenset(n.payload for n in notifies if n.channel == channel and n.payload)


class Listener:
    """Edge. One autocommit connection that has run LISTEN on `CHANNEL`."""

    def __init__(self, notifies: Callable[..., Iterator[_Notify]], close: Callable[[], None]) -> None:
        self._notifies = notifies
        self._close = close

    def wait(self, timeout: float) -> frozenset[str]:
        """The tables notified within `timeout` seconds: block until the first one, then take what else has already
        arrived. Empty when the timeout passes with none."""
        first = list(self._notifies(timeout=timeout, stop_after=1))
        if not first:
            return frozenset()
        return tables([*first, *self._notifies(timeout=0)])

    def close(self) -> None:
        self._close()


def listen(url: str) -> Listener | None:
    """Edge. A `Listener` on the store at `url`; None for sqlite, which has no NOTIFY."""
    conn = connect(url)
    if conn.dialect is not POSTGRES:
        conn.close()
        return None
    raw = cast(Any, conn.raw)  # psycopg's connection: Connection.raw is typed to the DB-API part both drivers share
    raw.execute(f"LISTEN {CHANNEL}")
    return Listener(raw.notifies, conn.close)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.store_notify", description=cast(str, __doc__).split("\n")[0])
    ap.add_argument("--store-url", required=True)
    ap.add_argument("--seconds", type=float, default=60.0)
    args = ap.parse_args(argv)
    listener = listen(args.store_url)
    if listener is None:
        print(f"{redact_url(args.store_url)}: sqlite has no NOTIFY; nothing to watch", file=sys.stderr)
        return 2
    deadline = datetime.now(UTC).timestamp() + args.seconds
    try:
        while (left := deadline - datetime.now(UTC).timestamp()) > 0:
            for table in sorted(listener.wait(left)):
                print(f"{_stamp()} {table}", flush=True)
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
