from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_tools import land_repo_lease, store_cli


@dataclass(frozen=True)
class Ran[T]:
    value: T
    epoch: int
    warning: str | None
    recovered_from: str | None = None


@dataclass(frozen=True)
class Refused:
    epoch: int | None
    holder: str | None


@dataclass(frozen=True)
class Unavailable:
    detail: str


LeaseCall = Callable[..., store_cli.LeaseResult]

TASK_LEASE_PREFIX = "land:task:"


def task_lease_name(initiative: str, task: str) -> str:
    """The lease name of a task land that says which initiative it belongs to; `parse_task_lease_name` reverses it."""
    return f"{TASK_LEASE_PREFIX}{initiative}/{task}"


def parse_task_lease_name(name: str) -> tuple[str, str] | None:
    """(initiative, task) for a `task_lease_name`; None for a bare `land:<task>`, a phase land, or a repo lease."""
    if not name.startswith(TASK_LEASE_PREFIX):
        return None
    initiative, slash, task = name.removeprefix(TASK_LEASE_PREFIX).partition("/")
    return (initiative, task) if slash and initiative and task else None


def _reason(result: store_cli.LeaseResult) -> str:
    if isinstance(result, store_cli.LeaseError):
        return result.detail
    if isinstance(result, store_cli.LeaseRefused):
        return f"held by {result.holder} at epoch {result.epoch}"
    if isinstance(result, store_cli.NotAvailable):
        return "no harness"
    return f"unexpected result {type(result).__name__}"


def _release_warning(name: str, result: store_cli.LeaseResult | Exception) -> str | None:
    if isinstance(result, store_cli.LeaseReleased):
        return None
    reason = f"{type(result).__name__}: {result}" if isinstance(result, Exception) else _reason(result)
    return f"warning: land lease {name} not released: {reason}"


def _release(give: LeaseCall, runs_dir: Path, name: str, holder: str, epoch: int) -> str | None:
    """Never raises an Exception: release runs on the failure path and must not mask the body's outcome."""
    try:
        return _release_warning(name, give(runs_dir=runs_dir, name=name, holder=holder, epoch=epoch))
    except Exception as exc:
        return _release_warning(name, exc)


def _took_over(refused: store_cli.LeaseRefused, taken_epoch: int) -> bool:
    """A takeover is granted at the refused epoch + 1; any other epoch means a live land took the lease first."""
    return refused.epoch is not None and taken_epoch == refused.epoch + 1


def run_under_land_lease[T](
    runs_dir: Path,
    task: str,
    holder: str,
    ttl: int,
    body: Callable[[int], T],
    acquire: LeaseCall | None = None,
    release: LeaseCall | None = None,
    epoch_now: Callable[[], int] | None = None,
    this_host: str | None = None,
    alive: Callable[[int], bool] = land_repo_lease.pid_alive,
    initiative: str | None = None,
) -> Ran[T] | Refused | Unavailable:
    """Refused and Unavailable run no body; a release warning rides on Ran, or as a note on the body's exception.

    `epoch_now`, when given, names the epoch to release at: a body that re-acquired an expired lease holds a newer one.

    With `this_host`, a refusal by a dead pid on that host gets one steal. A steal granted at any epoch but the refused
    one + 1 took a live land's lease: it is released and the refusal returned with no holder. `Ran.recovered_from`
    names the dead holder after a takeover.

    With `initiative`, the lease is named by `task_lease_name`, so a reader can tell which initiative's task it claims."""
    take = acquire or store_cli.lease_acquire
    give = release or store_cli.lease_release
    name = store_cli.land_lease_name(task) if initiative is None else task_lease_name(initiative, task)
    refused: store_cli.LeaseRefused | None = None
    try:
        got = take(runs_dir=runs_dir, name=name, holder=holder, ttl=ttl)
        if this_host is not None and isinstance(got, store_cli.LeaseRefused) and land_repo_lease.pid_gone(got.holder, this_host, alive):
            refused = got
            got = take(runs_dir=runs_dir, name=name, holder=holder, ttl=ttl, steal=True)
    except OSError as exc:
        # Only a failed spawn is not-available; a TypeError or other bug propagates.
        return Unavailable(f"{type(exc).__name__}: {exc}")
    if isinstance(got, store_cli.LeaseRefused):
        return Refused(got.epoch, got.holder)
    if not isinstance(got, store_cli.LeaseGranted):
        return Unavailable(_reason(got))
    if refused is not None and not _took_over(refused, got.epoch):
        _release(give, runs_dir, name, holder, got.epoch)
        return Refused(refused.epoch, None)

    def held() -> int:
        return got.epoch if epoch_now is None else epoch_now()

    try:
        value = body(got.epoch)
    except BaseException as exc:
        stuck = _release(give, runs_dir, name, holder, held())
        if stuck is not None:
            exc.add_note(stuck)
        raise
    return Ran(value, held(), _release(give, runs_dir, name, holder, held()), None if refused is None else refused.holder)
