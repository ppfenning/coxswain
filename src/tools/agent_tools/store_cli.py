"""The store's row writes, in process: thin glue from a `runs_dir` to `harness.store_api`.

Every function here resolves the store `run_store` resolves for `runs_dir` (or takes an explicit `store_url`), reads
the clock once, and calls one `harness.store_api` function. The result types are the API's own, re-exported so a
caller keeps writing `store_cli.StateSet`. Nothing spawns a child interpreter: a lane's checkout writes through its
own `harness`, never another checkout's (#1171, #1173).

`StateRefused`, `PauseRefused`, `LeaseRefused` and `NotInStore` are results, not errors. `NotAvailable` is a store
that could not be opened; `Failed` and `LeaseError` a statement that failed. Lease names are the caller's value,
except a land lease, named by land_lease_name.

`runner` is the one string-shaped door left: it runs a `harness.store_cli` command line (record-action, host verbs)
through that CLI's own `main`, in process, for callers that already hold such a command.

`harness` is imported on first use, here and in `run_store._api`: importing it loads the package's whole runtime, and
`agent_tools.chair` imports this module on `cox route chair status`'s startup path (test_cli_startup).
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_tools.run_store import _api, _store_url

if TYPE_CHECKING:  # the names `__getattr__` re-exports at run time, so a checker sees the classes a caller narrows on
    from harness.store_api import (
        Failed as Failed,
    )
    from harness.store_api import (
        Landed as Landed,
    )
    from harness.store_api import (
        LeaseError as LeaseError,
    )
    from harness.store_api import (
        LeaseGranted as LeaseGranted,
    )
    from harness.store_api import (
        LeaseRefused as LeaseRefused,
    )
    from harness.store_api import (
        LeaseReleased as LeaseReleased,
    )
    from harness.store_api import (
        LeaseResult,
        MarkLandedResult,
        MergeResult,
        PauseResult,
        ResumeResult,
        SetStateResult,
        WorkState,
    )
    from harness.store_api import (
        MergeRecorded as MergeRecorded,
    )
    from harness.store_api import (
        NotAvailable as NotAvailable,
    )
    from harness.store_api import (
        NotInStore as NotInStore,
    )
    from harness.store_api import (
        Paused as Paused,
    )
    from harness.store_api import (
        PauseRefused as PauseRefused,
    )
    from harness.store_api import (
        Resumed as Resumed,
    )
    from harness.store_api import (
        ResumeRefused as ResumeRefused,
    )
    from harness.store_api import (
        StateRefused as StateRefused,
    )
    from harness.store_api import (
        StateSet as StateSet,
    )

# The API's result types, re-exported on first use (PEP 562), so `from agent_tools.store_cli import StateSet` works.
_API_NAMES = frozenset({
    "Failed", "Landed", "LeaseError", "LeaseGranted", "LeaseRefused", "LeaseReleased", "LeaseResult", "MarkLandedResult",
    "MergeRecorded", "MergeResult", "NotAvailable", "NotInStore", "PauseRefused", "PauseResult", "Paused",
    "ResumeRefused", "ResumeResult", "Resumed", "SetStateResult", "StateRefused", "StateSet",
})  # fmt: skip


def __getattr__(name: str) -> Any:
    if name in _API_NAMES:
        return getattr(_api(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# The module a person or a remote host runs for the same writes: printed in a next-step hint, never run from here.
_MODULE = ["-m", "harness.store_cli"]
# The key `reply_row` answers a non-zero exit under, in place of a row.
STORE_ERROR = "store_error"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _url(runs_dir: Path) -> str:
    return _store_url(Path(runs_dir))


def land_lease_name(task: str) -> str:
    return f"land:{task}"


def runner(runs_dir: Path) -> Callable[[list[str]], tuple[int, str]]:
    """Edge. A function that runs `harness.store_cli <args>` in process against the store for `runs_dir`, returning
    its exit code and its stdout (its stderr when stdout is empty), as the child process used to."""
    from harness import store_cli as harness_store_cli

    url = _url(runs_dir)

    def run(args: list[str]) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        code = harness_store_cli.main([*args, "--store-url", url], out, err)
        return code, out.getvalue() or err.getvalue()

    return run


def reply_row(code: int, output: str) -> dict:
    """Pure. The row a store CLI call printed. A non-zero exit is `{STORE_ERROR: "store_cli exit N: <output>"}`, never a
    row, so a write the store did not take is reported; an unparseable or non-object reply on exit 0 is an empty row."""
    if code != 0:
        return {STORE_ERROR: " ".join(f"store_cli exit {code}: {output}".split())}
    try:
        record = json.loads(output)
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def mark_landed(runs_dir: Path, run_id: str, phase: str, task: str, pr: str, at: str) -> MarkLandedResult:
    return _api().mark_task_landed(_url(runs_dir), run_id, phase, task, pr, at, _now())


def mark_merged(runs_dir: Path, initiative: str, task: str, commit: str, by: str) -> MergeResult:
    """Edge. Record that an approved task's work is merged at `commit`; its state stays approved."""
    return _api().record_task_merge(_url(runs_dir), initiative, task, commit, by, _now())


def mark_phase_done(runs_dir: Path, run: str, phase: str, branch: str) -> MergeResult:
    """Edge. Mark the store's phase row done and merged into `branch`, never landed. Refused for a phase with no row."""
    return _api().record_phase_done(_url(runs_dir), run, phase, branch, _now())


def pause(store_url: str, run_id: str, reason: str | None = None) -> PauseResult:
    """Edge. `store_url` is the run's store, named explicitly: a run may be paused from a machine that never ran it."""
    return _api().pause_run(store_url, run_id, _now(), reason)


def resume(store_url: str, run_id: str) -> ResumeResult:
    return _api().resume_run(store_url, run_id, _now())


def set_state(
    runs_dir: Path, initiative: str, task: str, state: WorkState, by: str, expected: WorkState | None = None
) -> SetStateResult:
    return _api().set_work_state(_url(runs_dir), initiative, task, state, by, _now(), expected=expected)


def _warning(initiative: str, task: str, state: str, reason: str) -> str:
    return " ".join(f"warning: store did not record {initiative}/{task} as {state}: {reason}".split())


def state_write_failure(initiative: str, task: str, state: str, result: SetStateResult) -> str | None:
    """Pure. None when the store took the state; else one `warning:` line naming why. A store that could not be opened
    is a failure too: the write did not land."""
    from harness.store_api import Failed, NotAvailable, StateRefused  # on first use, as `_api` imports it

    if isinstance(result, StateRefused):
        return _warning(initiative, task, state, f"refused: {result.detail or 'no detail'}")
    if isinstance(result, Failed):
        return _warning(initiative, task, state, f"exit {result.code}: {result.detail}")
    if isinstance(result, NotAvailable) and result.reason:
        return _warning(initiative, task, state, result.reason)
    return None


def mirror_state(runs_dir: Path, initiative: str, task: str, state: WorkState, by: str) -> str | None:
    """Never raises. None when the store took the state; else one `warning:` line for the caller to print."""
    try:
        result = set_state(runs_dir, initiative, task, state, by)
    except Exception as exc:
        return _warning(initiative, task, state, f"{type(exc).__name__}: {exc}")
    return state_write_failure(initiative, task, state, result)


def lease_acquire(runs_dir: Path, name: str, holder: str, ttl: int, steal: bool = False) -> LeaseResult:
    return _api().acquire_lease(_url(runs_dir), name, holder, ttl, _now(), steal=steal)


def lease_renew(runs_dir: Path, name: str, holder: str, epoch: int, ttl: int) -> LeaseResult:
    return _api().renew_lease(_url(runs_dir), name, holder, epoch, ttl, _now())


def lease_release(runs_dir: Path, name: str, holder: str, epoch: int) -> LeaseResult:
    return _api().release_lease(_url(runs_dir), name, holder, epoch, _now())
