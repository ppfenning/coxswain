"""A lease kept alive while one long step runs under it: the branch lease a phase merge holds through its merge agent
and its gate (`landing_ops.merge_phase`).

The merge agent alone may take 450 s and the phase gate (the smoke) minutes more, so a merge can outlive the 900 s
it took the lease for, after which another writer could take the lease and push. `Keeper` renews the lease on a
thread of its own every `beat_s(ttl)` seconds while the step runs. `confirm` renews it once more, synchronously,
just before the step does what the lease protects (the push): only a renew the store took right then lets it go
ahead. A renew the store refuses (the lease lapsed, or another holder took it at a later epoch) loses the lease for
good: `store_lease.renew` never revives one. A renew that raised is not a loss on the thread, where the lease's own
expiry decides (the next good renew proves it is still held), but it is never a confirmation.

Time is an argument here too: the keeper is handed a clock and never reads one.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Literal

from harness import store_lease
from harness.store_dialect import Connection

__all__ = ["Keeper", "beat_s", "renew_step"]

RenewStep = Literal["held", "lost", "retry"]


def beat_s(ttl: int) -> float:
    """Pure. How often a lease of `ttl` seconds is renewed: three times in each lease, so two renews in a row may fail
    before it can lapse."""
    return max(ttl / 3, 0.05)


def renew_step(outcome: bool | None) -> RenewStep:
    """Pure. What one renew's answer means: `held` when the store took it, `lost` when it refused it (the lease is
    no longer this holder's at this epoch, or it lapsed), `retry` when the renew raised (None) and said nothing."""
    if outcome is None:
        return "retry"
    return "held" if outcome else "lost"


class Keeper:
    """Renews `name` (held by `holder` at `epoch`) for `ttl` seconds every `interval` seconds until stopped or lost.

    `conn` may be the caller's own connection: a `Connection` serialises the threads that share it. `lost` is set
    once a renew is refused, with `why`."""

    def __init__(self, conn: Connection, name: str, holder: str, epoch: int, *, ttl: int, clock: Callable[[], str],
                 interval: float | None = None) -> None:
        self._conn, self._name, self._holder, self._epoch, self._ttl = conn, name, holder, epoch, ttl
        self._clock = clock
        self._interval = beat_s(ttl) if interval is None else interval
        self._halt = threading.Event()
        self._lock = threading.Lock()  # one renew at a time: the thread's and `confirm`'s
        self.lost = threading.Event()
        self.why = ""
        self._thread = threading.Thread(target=self._loop, name=f"lease-keeper-{name}", daemon=True)

    def start(self) -> Keeper:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._halt.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)

    def confirm(self) -> bool:
        """Renew now: True only when the store took this renew, so the lease is this holder's for `ttl` more seconds."""
        return not self.lost.is_set() and self._renew() == "held"

    def _loop(self) -> None:
        while not self._halt.wait(self._interval):
            if self._renew() == "lost":
                return

    def _renew(self) -> RenewStep:
        with self._lock:
            if self.lost.is_set():
                return "lost"
            try:
                outcome: bool | None = store_lease.renew(self._conn, self._name, self._holder, self._epoch, self._clock(),
                                                         self._ttl)
            except Exception as exc:  # the store did not answer this time: the lease's own expiry decides
                outcome, error = None, f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"
            else:
                error = ""
            step = renew_step(outcome)
            if step == "lost":
                self.why = f"the lease {self._name} at epoch {self._epoch} is no longer {self._holder}'s"
                self.lost.set()
            elif step == "retry":
                self.why = f"the last renew of {self._name} raised: {error}"
            return step
